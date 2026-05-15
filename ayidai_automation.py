#!/usr/bin/env python3
"""
安易代「委外贷后管理」：默认图形界面完成认证、拉列表、查还款计划、发起划扣；日志仅在窗口底部显示。

启动：python ayidai_automation.py（或 gui）；子命令 fetch-all / plan / deduct 供脚本使用。
若出现 “macOS 26 … required, have instead 16 …” 并 abort：多为苹果自带 Python 3.9 的 Tk 与新版 macOS 不兼容，请用 Homebrew 的 python3.12+（brew install python@3.12）并设置 AYIDAI_PYTHON 后执行 ./run_gui.sh；IDE 内置终端若仍异常可改用「终端.app」。

打包分发：./build_dist.sh 或 pyinstaller AyidaiTool.spec → dist/AyidaiTool（无控制台）。

鉴权：认证页填写 Token/Cookie，或 Chrome CDP 读取（需 pip install playwright）。

自动化执行顺序与判定见 PROJECT_PLAN.md；接口入参/出参见 API_REFERENCE.md。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import requests

DEFAULT_BASE = "https://jxhbjawzkvjfy.jr-zbj.com"
# 与浏览器 DevTools 中真实请求一致；非浏览器 UA 可能被网关/WAF 直接拒绝。可用环境变量 AYIDAI_UA 覆盖。
DEFAULT_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GUI_LOGGER_NAME = "ayidai"


def app_writable_dir() -> str:
    """日志与导出文件默认可写目录：打包后为可执行文件所在目录，开发时为脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return _SCRIPT_DIR


def default_deduct_audit_log() -> str:
    return os.path.join(app_writable_dir(), "deduct_audit.log")


def resource_path(rel: str) -> str:
    """打包内资源（如 filter.example.json）；开发时相对脚本目录。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, rel)
    return os.path.join(_SCRIPT_DIR, rel)


class TkTextLogHandler(logging.Handler):
    """将日志写入 Tk 文本框（线程安全：通过 root.after 切回主线程）。"""

    def __init__(self, text_widget: scrolledtext.ScrolledText, root: tk.Tk) -> None:
        super().__init__(level=logging.INFO)
        self.text = text_widget
        self.root = root
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)

        def append() -> None:
            self.text.configure(state=tk.NORMAL)
            self.text.insert(tk.END, msg + "\n")
            self.text.see(tk.END)
            self.text.configure(state=tk.DISABLED)

        try:
            self.root.after(0, append)
        except tk.TclError:
            pass


LIST_PATH = "/prod-api/system/postLoan/wbOverdueOrder"
PLAN_PATH = "/prod-api/system/sysCreditRepaymentPlan/postLoan/getOrderPlan"
DEDUCT_PATH = "/prod-api/system/creditOrderDetailed/single/deduct"


def order_list_row_pk(row: dict[str, Any]) -> Any | None:
    """委外列表 `data[]` 行里的订单主键。接口可能返回 `id` 或 `orderId`（与还款计划查询参数 id 一致）。"""
    if row.get("id") is not None:
        return row.get("id")
    return row.get("orderId")


def customer_dedup_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """按客户去重：有 customerId 则一户一行；否则按订单主键+授信号视为独立「户」。"""
    cid = row.get("customerId")
    if cid is not None and str(cid).strip() != "":
        return ("customer", cid)
    pk = order_list_row_pk(row)
    co = row.get("creditOrder") or ""
    return ("order", pk, co)


def order_row_skip_as_repaid_list(row: dict[str, Any]) -> bool:
    """列表行是否视为已还/无待还：跳过整单。"""
    if bool(row.get("repayStatus")):
        return True
    try:
        oa = row.get("outstandingAmount")
        if oa is not None and float(oa) <= 0:
            tu = row.get("totalOverdueAmount")
            if tu is None or float(tu) <= 0:
                return True
    except (TypeError, ValueError):
        pass
    return False


def order_row_is_hopeless(row: dict[str, Any], skip_overdue_days: int = 90) -> tuple[bool, str]:
    """判断订单是否「无望划扣」——长期余额不足且逾期很久，不值得反复尝试。"""
    fail_desc = (row.get("failDesc") or "").strip()
    try:
        overdue_days = int(row.get("overdueDays") or 0)
    except (TypeError, ValueError):
        overdue_days = 0
    if "余额不足" in fail_desc and overdue_days >= skip_overdue_days:
        return True, f"逾期{overdue_days}天且余额不足，大概率无望"
    return False, ""


def order_row_is_overdue_list(row: dict[str, Any]) -> bool:
    """列表行是否仍有逾期（用于「只处理第一笔逾期单」）。"""
    try:
        od = row.get("overdueDays")
        if od is not None and int(od) > 0:
            return True
    except (TypeError, ValueError):
        pass
    try:
        md = row.get("maxOverdueDays")
        if md is not None and int(md) > 0:
            return True
    except (TypeError, ValueError):
        pass
    try:
        tu = row.get("totalOverdueAmount")
        if tu is not None and float(tu) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


def plan_row_skip_deduct(plan: dict[str, Any]) -> bool:
    """还款计划行是否无需再划扣。

    前端 disabled 条件第一项就是 state==2。
    已成功 / 正在进行的批扣也不需重复操作。
    """
    if plan.get("state") == 2:
        return True
    dr = str(plan.get("deductResult") or "").strip()
    # 明确成功
    if dr in ("批扣成功", "主动还款成功", "划扣成功", "提前结清", "逾期批扣成功"):
        return True
    # 进行中 — 避免重复提交
    if "扣款中" in dr or "划扣中" in dr:
        return True
    return False


def _deduct_amount_from_plan(plan: dict[str, Any]) -> float | None:
    """提取应传的 repayAmount。

    canRepayAmount 有值时优先使用（匹配前端弹出金额输入框的行为）；
    为空时回退到 remainTotalAmount（部分产品 canRepayAmount 为 null 但后端仍要求金额）。
    """
    can_repay = plan.get("canRepayAmount")
    if can_repay is not None:
        try:
            v = float(can_repay)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    remain = plan.get("remainTotalAmount")
    if remain is not None:
        try:
            v = float(remain)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return None


def order_display_name(order: dict[str, Any]) -> str:
    """格式化订单可读名称用于日志。"""
    name = order.get("customerName") or order.get("realName") or "?"
    phone = order.get("customerPhone") or ""
    product = order.get("productName") or ""
    order_no = order.get("orderNo") or order.get("creditOrder") or ""
    return f"{name}({phone}) {product} {order_no}"


def _plan_repayment_no_sort_key(plan: dict[str, Any]) -> tuple[int, int]:
    try:
        return (0, int(plan.get("repaymentNo")))
    except (TypeError, ValueError):
        return (1, 0)


def pick_deductible_plan_row(
    plans: list[dict[str, Any]],
) -> tuple[int, dict[str, Any]] | None:
    """
    根据前端「发起划扣」按钮可用性逻辑选取可划扣的期次:

    前端按钮 disabled 条件:
      disabled: state==2 || canSaveId != row.id || repaymentDate > now

    canSaveId = 第一个逾期期次(state=3)的 id，后端/前端均按此规则确定。
    即: 按 repaymentNo 升序找到第一个 state=3 且还款日 <= 今天的期次。
    """
    sorted_plans = sorted(enumerate(plans, 1), key=lambda t: _plan_repayment_no_sort_key(t[1]))

    now_date = datetime.now().date()

    for plan_idx, plan in sorted_plans:
        if plan.get("id") is None:
            continue
        # 条件1: state==2 → 已还款，跳过
        if plan.get("state") == 2:
            continue
        # 条件3: repaymentDate > now → 未到还款日，跳过（同时也是 canSaveId 还没轮到）
        rd = plan.get("repaymentDate")
        if rd:
            try:
                plan_date = datetime.strptime(str(rd)[:10], "%Y-%m-%d").date()
                if plan_date > now_date:
                    continue
            except ValueError:
                pass
        # 跳过已成功划扣的
        if plan_row_skip_deduct(plan):
            continue
        # 找到第一个满足条件的 → 这就是 canSaveId 对应的期次
        return (plan_idx, plan)

    return None


def find_plan_by_repayment_no(
    plans: list[dict[str, Any]], repayment_no: int
) -> tuple[int, dict[str, Any]] | None:
    for plan_idx, plan in enumerate(plans, 1):
        try:
            if int(plan.get("repaymentNo")) != repayment_no:
                continue
        except (TypeError, ValueError):
            continue
        if plan.get("id") is None:
            continue
        return (plan_idx, plan)
    return None


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def host_only(base_url: str) -> str:
    netloc = urlparse(base_url).netloc
    return netloc.split(":")[0] if netloc else ""


def parse_cookie_value(cookie_header: str, name: str) -> str | None:
    """从浏览器「复制为 cURL」或 Application 里复制的 Cookie 字符串中解析键值。"""
    if not cookie_header or not cookie_header.strip():
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip() == name:
            return v.strip()
    return None


def resolve_token(direct_token: str, cookie_header: str) -> str:
    t = (direct_token or "").strip()
    if t:
        return t
    from_cookie = parse_cookie_value(cookie_header, "Admin-Token")
    return (from_cookie or "").strip()


def apply_cookie_header(s: requests.Session, base_url: str, cookie_header: str) -> None:
    """把 `a=b; c=d` 写入会话 Cookie（域名取 base_url 的 host）。"""
    host = host_only(base_url)
    if not host or not cookie_header.strip():
        return
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            s.cookies.set(k, v, domain=host, path="/")


def try_token_from_chrome_cdp(cdp_http_url: str, api_base: str) -> str | None:
    """
    连接本机已开启远程调试的 Chrome，读取 Admin-Token。
    需：Chrome 已用远程调试（如 chrome://inspect/#remote-debugging 或 --remote-debugging-port=9222），
    且已安装：pip install playwright
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    target_host = host_only(api_base)
    candidates: list[tuple[str, str]] = []

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_http_url)
        try:
            for ctx in browser.contexts:
                for c in ctx.cookies():
                    if c.get("name") != "Admin-Token":
                        continue
                    dom = (c.get("domain") or "").lstrip(".")
                    val = (c.get("value") or "").strip()
                    if val:
                        candidates.append((dom, val))
        finally:
            browser.close()

    if not candidates:
        return None

    def domain_matches(dom: str, host: str) -> bool:
        if not host:
            return True
        return dom == host or host.endswith("." + dom) or dom.endswith(host)

    for dom, val in candidates:
        if domain_matches(dom, target_host):
            return val
    return candidates[0][1]


def build_session(base_url: str, token: str, extra_cookie_header: str = "") -> requests.Session:
    s = requests.Session()
    ua = os.environ.get("AYIDAI_UA", "").strip() or DEFAULT_BROWSER_UA
    s.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "device": "1",
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "user-agent": ua,
            "origin": base_url.rstrip("/"),
            "referer": f"{base_url.rstrip('/')}/afterLoanMag/outrepayment",
        }
    )
    # 与 Chrome 同源 XHR 一致；若遇兼容问题可设 AYIDAI_MINIMAL_HEADERS=1 去掉以下三项
    if os.environ.get("AYIDAI_MINIMAL_HEADERS", "").strip().lower() not in ("1", "true", "yes", "on"):
        s.headers["sec-fetch-dest"] = "empty"
        s.headers["sec-fetch-mode"] = "cors"
        s.headers["sec-fetch-site"] = "same-origin"
    if extra_cookie_header.strip():
        apply_cookie_header(s, base_url, extra_cookie_header)
    if not any(c.name == "Admin-Token" for c in s.cookies):
        h = host_only(base_url)
        if h:
            s.cookies.set("Admin-Token", token, domain=h, path="/")
        else:
            s.cookies.set("Admin-Token", token, path="/")
    return s


def api_post_json(
    s: requests.Session, base: str, path: str, body: dict[str, Any]
) -> dict[str, Any]:
    s.headers["timestamp"] = _now_ms()
    s.headers["content-type"] = "application/json;charset=UTF-8"
    r = s.post(base.rstrip("/") + path, json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def api_get(s: requests.Session, base: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    s.headers["timestamp"] = _now_ms()
    if "content-type" in s.headers:
        del s.headers["content-type"]
    r = s.get(base.rstrip("/") + path, params=params or {}, timeout=60)
    r.raise_for_status()
    return r.json()


def fetch_all_orders(
    s: requests.Session,
    base: str,
    filter_template: dict[str, Any],
    page_size: int,
    log: logging.Logger,
) -> list[dict[str, Any]]:
    """分页拉齐列表。若响应里无 total 或 total<=0，则按「本页条数 < size 或本页为空」停页，避免误停在第一页。"""
    page = 1
    all_rows: list[dict[str, Any]] = []
    max_pages = 5000

    while True:
        if page > max_pages:
            log.warning("分页已达上限 %s 页，停止拉取（请检查接口或缩小筛选）", max_pages)
            break
        body = {**filter_template, "page": page, "size": page_size}
        data = api_post_json(s, base, LIST_PATH, body)
        if data.get("code") != 200:
            raise RuntimeError(f"列表接口异常: {data}")
        raw_total = data.get("total")
        total_positive: int | None = None
        if raw_total is not None and raw_total != "":
            try:
                t = int(raw_total)
                if t > 0:
                    total_positive = t
            except (TypeError, ValueError):
                pass
        chunk = data.get("data") or []
        if total_positive is not None:
            log.info(
                "拉取第 %s 页，本页 %s 条，累计 %s / total=%s",
                page,
                len(chunk),
                len(all_rows) + len(chunk),
                total_positive,
            )
        else:
            log.info(
                "拉取第 %s 页，本页 %s 条，累计 %s（接口 total 为空或 0，按页大小判断是否还有下一页）",
                page,
                len(chunk),
                len(all_rows) + len(chunk),
            )
        all_rows.extend(chunk)
        if not chunk:
            break
        if total_positive is not None and len(all_rows) >= total_positive:
            break
        if total_positive is None and len(chunk) < page_size:
            break
        page += 1

    return all_rows


def get_repayment_plans(
    s: requests.Session,
    base: str,
    order_id: int,
    credit_order: str,
    customer_id: str = "",
    page: int = 1,
    size: int = 100,
) -> list[dict[str, Any]]:
    params = {
        "creditOrder": credit_order,
        "id": order_id,
        "customerId": customer_id,
        "page": page,
        "size": size,
    }
    data = api_get(s, base, PLAN_PATH, params)
    if data.get("code") != 200:
        raise RuntimeError(f"还款计划异常: {data}")
    return data.get("data") or []


def parse_deduct_start_repayment_no(msg: Any) -> int | None:
    """从报错文案中提取“发起期数[n]”的 n。"""
    s = str(msg or "")
    m = re.search(r"发起期数\[(\d+)\]", s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def single_deduct(
    s: requests.Session,
    base: str,
    plan_row_id: int,
    dry_run: bool,
    repay_amount: float | None = None,
) -> dict[str, Any]:
    """
    发起单笔划扣。默认连续 GET 两次（对齐前端双击确定行为）。
    若 repay_amount 不为 None，追加 &repayAmount= 参数。
    """
    if dry_run:
        params_str = f"id={plan_row_id}"
        if repay_amount is not None:
            params_str += f"&repayAmount={repay_amount}"
        return {"dry_run": True, "would_call": f"GET {DEDUCT_PATH}?{params_str}"}

    no_retry = os.environ.get("AYIDAI_DEDUCT_NO_RETRY", "").strip().lower() in ("1", "true", "yes", "on")

    def _call(amt: float | None) -> dict[str, Any]:
        params: dict[str, Any] = {"id": plan_row_id}
        if amt is not None:
            params["repayAmount"] = amt
        return api_get(s, base, DEDUCT_PATH, params)

    first = _call(repay_amount)
    if no_retry:
        return first

    delay_s = 0.35
    raw_delay = os.environ.get("AYIDAI_DEDUCT_RETRY_DELAY", "").strip()
    if raw_delay:
        try:
            delay_s = max(0.0, float(raw_delay))
        except ValueError:
            pass

    msg1 = str(first.get("msg") or "")
    force_second = os.environ.get("AYIDAI_DEDUCT_FORCE_SECOND", "").strip().lower() in ("1", "true", "yes", "on")
    need_second = force_second or ("重复提交" in msg1) or ("操作频繁" in msg1)
    if not need_second:
        return first

    time.sleep(delay_s)
    second = _call(repay_amount)

    if second.get("code") == 200:
        second["_deduct_first_attempt"] = first
        return second
    if first.get("code") == 200:
        first["_deduct_second_attempt"] = second
        return first
    second["_deduct_first_attempt"] = first
    return second


def append_deduct_audit(log_path: str | None, record: dict[str, Any]) -> None:
    """追加一行 JSON（代扣审计）；log_path 为空则跳过。"""
    if not log_path or not str(log_path).strip():
        return
    path = os.path.abspath(os.path.expanduser(log_path.strip()))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_cookie_arg(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("@") and len(raw) > 1:
        with open(raw[1:], encoding="utf-8") as f:
            return f.read().strip()
    return raw


def cmd_fetch_all(args: argparse.Namespace, log: logging.Logger) -> int:
    cookie = load_cookie_arg(getattr(args, "cookie", "") or "")
    token = resolve_token(os.environ.get("ADMIN_TOKEN", "").strip(), cookie)
    if not token:
        log.error("请设置环境变量 ADMIN_TOKEN，或使用 --cookie 粘贴含 Admin-Token= 的 Cookie")
        return 1
    with open(args.filter, encoding="utf-8") as f:
        filt = json.load(f)
    s = build_session(args.base, token, extra_cookie_header=cookie)
    rows = fetch_all_orders(s, args.base, filt, args.page_size, log)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    log.info("已写入 %s，共 %s 条", args.out, len(rows))
    return 0


def cmd_test_one(args: argparse.Namespace, log: logging.Logger) -> int:
    """测试单个订单：拉取还款计划 → canSaveId 命中 → 确认 → 划扣。"""
    cookie = load_cookie_arg(getattr(args, "cookie", "") or "")
    token = resolve_token(os.environ.get("ADMIN_TOKEN", "").strip(), cookie)
    if not token:
        log.error("请设置环境变量 ADMIN_TOKEN，或使用 --cookie 粘贴含 Admin-Token= 的 Cookie")
        return 1

    s = build_session(args.base, token, extra_cookie_header=cookie)

    # 1. 拉取还款计划
    log.info("拉取还款计划: orderId=%s creditOrder=%s", args.order_id, args.credit_order)
    plans = get_repayment_plans(
        s, args.base, args.order_id, args.credit_order,
        customer_id=getattr(args, "customer_id", "") or "",
        size=100,
    )

    # 2. 汇总
    repaid = sum(1 for p in plans if p.get("state") == 2)
    overdue = sum(1 for p in plans if p.get("state") == 3)
    log.info("共 %s 期: 已还=%s  逾期=%s  其他=%s", len(plans), repaid, overdue, len(plans) - repaid - overdue)

    # 3. 打印每期详情
    for p in plans:
        log.info(
            "  期次=%s id=%-10s 还款日=%-12s stateText=%-6s remain=%s canRepay=%s deductResult=%s",
            p.get("repaymentNo"),
            p.get("id"),
            p.get("repaymentDate"),
            p.get("stateText"),
            p.get("remainTotalAmount"),
            p.get("canRepayAmount"),
            p.get("deductResult"),
        )

    # 4. canSaveId 命中
    picked = pick_deductible_plan_row(plans)
    if not picked:
        log.warning("无可划扣期次（已无逾期或未到还款日）")
        return 1

    plan_idx, plan = picked
    plan_id = plan.get("id")
    repayment_no = plan.get("repaymentNo")

    log.info("---")
    log.info("canSaveId 命中: 期次=%s  plan行id=%s  还款日=%s", repayment_no, plan_id, plan.get("repaymentDate"))
    log.info("  stateText=%s  deductResult=%s", plan.get("stateText"), plan.get("deductResult"))
    log.info("  remainTotalAmount=%s  canRepayAmount=%s", plan.get("remainTotalAmount"), plan.get("canRepayAmount"))

    deduct_amount = _deduct_amount_from_plan(plan)
    if deduct_amount is not None:
        src = "canRepayAmount" if (plan.get("canRepayAmount") is not None and float(plan.get("canRepayAmount") or 0) > 0) else "remainTotalAmount"
        log.info("  %s=%s → repayAmount=%s", src, plan.get(src), deduct_amount)
    else:
        log.info("  无需传金额（canRepayAmount/remainTotalAmount 均为空）")

    # 5. 确认
    if args.dry_run:
        log.info("[DRY RUN] 不会实际发起划扣")
        return 0

    if not args.yes:
        prompt = f"\n确认对期次 {repayment_no} (id={plan_id}) 发起划扣？"
        if deduct_amount is not None:
            prompt += f" 金额={deduct_amount}元"
        prompt += "\n输入 y 确认，其他键取消: "
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            log.info("已取消")
            return 0
        if answer != "y":
            log.info("已取消")
            return 0

    # 6. 发起划扣
    log.info("发起划扣: plan_id=%s repayAmount=%s", plan_id, deduct_amount)
    audit_path = os.environ.get("AYIDAI_DEDUCT_LOG") or default_deduct_audit_log()
    rec: dict[str, Any] = {
        "time": datetime.now(timezone.utc).astimezone().isoformat(),
        "action": "single_deduct",
        "endpoint": DEDUCT_PATH,
        "plan_row_id": plan_id,
        "dry_run": False,
        "base": args.base,
        "order_id": args.order_id,
        "credit_order": args.credit_order,
        "repayment_no": repayment_no,
    }
    if deduct_amount is not None:
        rec["repay_amount"] = deduct_amount

    try:
        res = single_deduct(s, args.base, int(plan_id), False, repay_amount=deduct_amount)
        rec["result"] = res
        rec["ok"] = bool(res.get("code") == 200)
        append_deduct_audit(audit_path, rec)
        if rec["ok"]:
            log.info("划扣成功: %s", res.get("msg"))
        else:
            log.warning("划扣失败: code=%s msg=%s", res.get("code"), res.get("msg"))
            hint = res.get("_deduct_hint")
            if hint:
                log.warning("  %s", hint)
        return 0 if rec["ok"] else 1
    except Exception as e:
        rec["ok"] = False
        rec["error"] = repr(e)
        append_deduct_audit(audit_path, rec)
        log.exception("划扣请求异常")
        return 1


def cmd_deduct(args: argparse.Namespace, log: logging.Logger) -> int:
    cookie = load_cookie_arg(getattr(args, "cookie", "") or "")
    token = resolve_token(os.environ.get("ADMIN_TOKEN", "").strip(), cookie)
    if not token:
        log.error("请设置环境变量 ADMIN_TOKEN，或使用 --cookie 粘贴含 Admin-Token= 的 Cookie")
        return 1
    audit_path: str | None = None
    if not getattr(args, "no_audit_log", False):
        audit_path = (getattr(args, "audit_log", None) or "").strip() or None

    rec: dict[str, Any] = {
        "time": datetime.now(timezone.utc).astimezone().isoformat(),
        "action": "single_deduct",
        "endpoint": DEDUCT_PATH,
        "plan_row_id": args.plan_id,
        "dry_run": bool(args.dry_run),
        "base": args.base,
    }
    oid = getattr(args, "order_id", None)
    if oid is not None:
        rec["order_id"] = oid
    co = getattr(args, "credit_order", None)
    if co:
        rec["credit_order"] = co
    ra = getattr(args, "repay_amount", None)
    if ra is not None:
        rec["repay_amount"] = float(ra)

    s = build_session(args.base, token, extra_cookie_header=cookie)
    try:
        res = single_deduct(s, args.base, args.plan_id, args.dry_run, repay_amount=ra)
    except Exception as e:
        rec["ok"] = False
        rec["error"] = repr(e)
        append_deduct_audit(audit_path, rec)
        log.exception("划扣请求异常（已写入审计日志）")
        return 1

    rec["result"] = res
    if res.get("dry_run"):
        rec["ok"] = True
    else:
        rec["ok"] = res.get("code") == 200

    append_deduct_audit(audit_path, rec)
    log.info("划扣结果: %s", res)
    if audit_path:
        log.info("代扣审计已追加: %s", os.path.abspath(audit_path))

    if res.get("dry_run"):
        return 0
    return 0 if res.get("code") == 200 else 1


def cmd_plan(args: argparse.Namespace, log: logging.Logger) -> int:
    cookie = load_cookie_arg(getattr(args, "cookie", "") or "")
    token = resolve_token(os.environ.get("ADMIN_TOKEN", "").strip(), cookie)
    if not token:
        log.error("请设置环境变量 ADMIN_TOKEN，或使用 --cookie 粘贴含 Admin-Token= 的 Cookie")
        return 1
    s = build_session(args.base, token, extra_cookie_header=cookie)
    plans = get_repayment_plans(s, args.base, args.order_id, args.credit_order, size=args.size)

    repaid = sum(1 for p in plans if p.get("state") == 2)
    overdue = sum(1 for p in plans if p.get("state") == 3)
    log.info("共 %s 期: 已还=%s 逾期=%s 其他=%s", len(plans), repaid, overdue, len(plans) - repaid - overdue)

    for p in plans:
        log.info(
            "  期次=%s id=%s 还款日=%s stateText=%s deductResult=%s",
            p.get("repaymentNo"),
            p.get("id"),
            p.get("repaymentDate"),
            p.get("stateText"),
            p.get("deductResult"),
        )

    picked = pick_deductible_plan_row(plans)
    if picked:
        _, plan = picked
        log.info(
            "canSaveId 命中: 期次=%s id=%s 还款日=%s",
            plan.get("repaymentNo"),
            plan.get("id"),
            plan.get("repaymentDate"),
        )
    else:
        log.info("无可划扣期次（已无逾期或未到还款日）")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(plans, f, ensure_ascii=False, indent=2)
        log.info("已写入 %s", args.out)
    return 0


def _default_filter_json_path() -> str:
    p = resource_path("filter.example.json")
    return p if os.path.isfile(p) else os.path.join(app_writable_dir(), "filter.example.json")


class AyidaiGuiApp:
    """图形界面：认证、拉列表、查计划、划扣；日志仅写入底部窗口（不依赖控制台）。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("安易代 · 委外助手")
        root.geometry("920x760")
        root.minsize(820, 640)

        self.token_var = tk.StringVar(value=os.environ.get("ADMIN_TOKEN", ""))
        self.base_var = tk.StringVar(value=DEFAULT_BASE)
        self.cdp_var = tk.StringVar(value=os.environ.get("CHROME_CDP_URL", "http://127.0.0.1:9222"))
        self.hide_token = tk.BooleanVar(value=False)
        self.per_customer_first_overdue_order = tk.BooleanVar(value=True)
        self._stop_event = threading.Event()

        self._build_main_page()
        self._setup_gui_logger()
        self.logger.info("程序已启动（所有操作日志显示在本区域，无需控制台）。")

    def _setup_gui_logger(self) -> None:
        self.logger = logging.getLogger(GUI_LOGGER_NAME)
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        self.logger.propagate = False
        self.logger.addHandler(TkTextLogHandler(self.log_box, self.root))

    def _clear_log(self) -> None:
        self.log_box.configure(state=tk.NORMAL)
        self.log_box.delete("1.0", tk.END)
        self.log_box.configure(state=tk.DISABLED)

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("文本", "*.txt"), ("全部", "*")],
            initialdir=app_writable_dir(),
            initialfile="ayidai_gui_log.txt",
        )
        if not path:
            return
        self.log_box.configure(state=tk.NORMAL)
        content = self.log_box.get("1.0", tk.END)
        self.log_box.configure(state=tk.DISABLED)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        self.logger.info("日志已保存: %s", path)
        messagebox.showinfo("已保存", path)

    def _auth_triple(self) -> tuple[str, str, str]:
        cookie = self.cookie_box.get("1.0", tk.END).strip()
        token = resolve_token(self.token_var.get().strip(), cookie)
        return token, cookie, self.base_var.get().strip()

    def _require_auth(self) -> tuple[str, str, str] | None:
        t, c, b = self._auth_triple()
        if not t:
            messagebox.showerror("认证", "请在「认证」页填写 Admin-Token，或粘贴含 Admin-Token= 的 Cookie。")
            return None
        os.environ["ADMIN_TOKEN"] = t
        return t, c, b

    def _run_in_thread(self, fn: Callable[[], None]) -> None:
        def target() -> None:
            try:
                fn()
            except Exception:
                self.logger.exception("任务异常")

        threading.Thread(target=target, daemon=True).start()

    def _build_main_page(self) -> None:
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(9, weight=1)

        ttk.Label(main, text="API 根地址", font=("", 10, "bold")).grid(row=0, column=0, sticky="nw", pady=(0, 2))
        ttk.Entry(main, textvariable=self.base_var).grid(row=0, column=1, columnspan=2, sticky="we", pady=(0, 2))

        ttk.Label(main, text="Admin-Token").grid(row=1, column=0, sticky="nw", pady=2)
        self.token_entry = ttk.Entry(main, textvariable=self.token_var)
        self.token_entry.grid(row=1, column=1, sticky="we", pady=2)

        def sync_mask(*_a: Any) -> None:
            self.token_entry.configure(show="*" if self.hide_token.get() else "")

        ttk.Checkbutton(main, text="隐藏 Token", variable=self.hide_token, command=sync_mask).grid(
            row=1, column=2, sticky="w", padx=4
        )
        sync_mask()

        ttk.Label(main, text="Cookie（可选）").grid(row=2, column=0, sticky="nw", pady=2)
        self.cookie_box = scrolledtext.ScrolledText(main, height=4, wrap=tk.WORD)
        self.cookie_box.grid(row=2, column=1, columnspan=2, sticky="we", pady=2)
        ttk.Label(
            main,
            text="可粘贴整段 Cookie（含 Admin-Token、acw_tc 等）。仅填此项也可登录。",
            wraplength=640,
        ).grid(row=3, column=1, columnspan=2, sticky="w")

        ttk.Label(main, text="Chrome CDP").grid(row=4, column=0, sticky="nw", pady=4)
        cdp_row = ttk.Frame(main)
        cdp_row.grid(row=4, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Entry(cdp_row, textvariable=self.cdp_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(cdp_row, text="从已登录 Chrome 读取 Token", command=self._read_token_cdp).pack(
            side=tk.LEFT, padx=(8, 0)
        )

        ttk.Separator(main, orient="horizontal").grid(row=5, column=0, columnspan=3, sticky="we", pady=12)
        ttk.Checkbutton(
            main,
            text="每户仅处理「列表顺序下第一笔逾期订单」，且该单只划扣一期（已还/无欠款跳过；取消勾选=全量多期）",
            variable=self.per_customer_first_overdue_order,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.start_btn = ttk.Button(main, text="开始一键处理", command=self._start_auto)
        self.start_btn.grid(row=7, column=0, columnspan=2, sticky="we", pady=(0, 12), ipady=8, padx=(0, 4))
        self.stop_btn = ttk.Button(main, text="中断处理", command=self._stop_auto)
        self.stop_btn.grid(row=7, column=2, sticky="we", pady=(0, 12), ipady=8, padx=(4, 0))
        self.stop_btn.grid_remove()  # 默认隐藏
        ttk.Separator(main, orient="horizontal").grid(row=8, column=0, columnspan=3, sticky="we", pady=(0, 4))

        log_frame = ttk.Frame(main)
        log_frame.grid(row=9, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)

        log_bar = ttk.Frame(log_frame)
        log_bar.grid(row=0, column=0, sticky="we")
        ttk.Label(log_bar, text="操作日志").pack(side=tk.LEFT)
        ttk.Button(log_bar, text="清空日志", command=self._clear_log).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(log_bar, text="保存日志到文件…", command=self._save_log).pack(side=tk.RIGHT)

        _mono = ("Consolas", 11) if sys.platform == "win32" else ("Menlo", 11)
        self.log_box = scrolledtext.ScrolledText(log_frame, height=12, state=tk.DISABLED, wrap=tk.WORD, font=_mono)
        self.log_box.grid(row=1, column=0, sticky="nsew")

    def _read_token_cdp(self) -> None:
        try:
            import playwright  # noqa: F401
        except ImportError:
            messagebox.showerror("缺少依赖", "请先安装：pip install playwright")
            return
        base = self.base_var.get().strip()
        cdp = self.cdp_var.get().strip()
        self.logger.info("连接 CDP: %s", cdp)

        def work() -> None:
            try:
                t = try_token_from_chrome_cdp(cdp, base)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("CDP 失败", str(e)))
                self.logger.error("CDP 失败: %s", e)
                return
            if not t:
                self.root.after(
                    0,
                    lambda: messagebox.showwarning("未找到", "浏览器 Cookie 中无 Admin-Token，请确认已登录。"),
                )
                return

            def apply() -> None:
                self.token_var.set(t)
                os.environ["ADMIN_TOKEN"] = t
                self.logger.info("已从 Chrome 填入 Admin-Token（长度 %s）", len(t))
                messagebox.showinfo("成功", "已填入 Admin-Token")

            self.root.after(0, apply)

        self._run_in_thread(work)

    def _stop_auto(self) -> None:
        """中断正在进行的处理任务。"""
        self._stop_event.set()
        self.logger.info("===== 收到中断请求，正在停止… =====")
        self.stop_btn.configure(state=tk.DISABLED, text="正在停止…")

    def _start_auto(self) -> None:
        auth = self._require_auth()
        if not auth:
            return
        token, cookie, base = auth

        filter_path = _default_filter_json_path()
        if not os.path.isfile(filter_path):
            messagebox.showerror("文件不存在", f"筛选 JSON 文件不存在: {filter_path}")
            return

        audit_path = default_deduct_audit_log()
        page_size = 50
        per_customer_first = self.per_customer_first_overdue_order.get()

        self._stop_event.clear()
        self.start_btn.grid_remove()
        self.stop_btn.grid()
        self.stop_btn.configure(state=tk.NORMAL, text="中断处理")

        def work() -> None:
            log = logging.getLogger(GUI_LOGGER_NAME)
            stop = self._stop_event  # 本地引用，避免重复 self. 访问
            try:
                with open(filter_path, encoding="utf-8") as f:
                    filt = json.load(f)
                s = build_session(base, token, extra_cookie_header=cookie)

                log.info("===== 开始拉取订单列表 =====")
                if per_customer_first:
                    log.info(
                        "模式：按客户去重 — 每户仅第一笔逾期订单，且该单只划扣一期（取消勾选=全量订单+多期）"
                    )
                orders = fetch_all_orders(s, base, filt, page_size, log)
                total_orders = len(orders)
                log.info("共拉取 %s 笔订单", total_orders)

                total_plans = 0
                success_count = 0
                skip_count = 0
                fail_count = 0
                hopeless_count = 0
                customers_touched: set[tuple[Any, ...]] = set()
                deduct_attempts = 0
                product_stats: dict[str, dict[str, int]] = {}  # {product: {ok/fail/skip}}
                max_attempts_per_order = int(os.environ.get("AYIDAI_MAX_DEDUCT_ATTEMPTS_PER_ORDER", "3"))

                for idx, order in enumerate(orders, 1):
                    if stop.is_set():
                        log.info("===== 用户中断，已停止处理 =====")
                        break
                    order_success = 0
                    order_skip = 0
                    order_fail = 0
                    if order_row_skip_as_repaid_list(order):
                        log.info("[%s/%s] %s 列表判定已还/无欠款，跳过整单", idx, total_orders, order_display_name(order))
                        continue
                    if per_customer_first and not order_row_is_overdue_list(order):
                        log.info("[%s/%s] %s 列表无逾期字段，跳过", idx, total_orders, order_display_name(order))
                        continue
                    hopeless, reason = order_row_is_hopeless(order)
                    if hopeless:
                        hopeless_count += 1
                        pn = order.get("productName") or "?"
                        product_stats.setdefault(pn, {"ok": 0, "fail": 0, "skip": 0})["skip"] += 1
                        log.info("[%s/%s] %s %s，跳过", idx, total_orders, order_display_name(order), reason)
                        continue

                    oid = order_list_row_pk(order)
                    credit_order = order.get("creditOrder") or ""
                    if not oid or not credit_order:
                        log.info("[%s/%s] %s 缺少 id/orderId 或 creditOrder，跳过", idx, total_orders, order_display_name(order))
                        continue

                    if per_customer_first:
                        ck = customer_dedup_key(order)
                        if ck in customers_touched:
                            log.info(
                                "[%s/%s] %s 该客户已处理，跳过",
                                idx, total_orders, order_display_name(order),
                            )
                            continue
                        customers_touched.add(ck)

                    order_label = order_display_name(order)
                    log.info("[%s/%s] %s", idx, total_orders, order_label)
                    try:
                        cust = order.get("customerId")
                        cid_str = "" if cust is None else str(cust).strip()
                        plans = get_repayment_plans(
                            s, base, int(oid), str(credit_order), customer_id=cid_str, size=100
                        )
                    except Exception as e:
                        log.error("[%s/%s] %s 查询还款计划失败: %s", idx, total_orders, order_label, e)
                        continue

                    if not plans:
                        log.info("[%s/%s] %s 无还款计划，跳过", idx, total_orders, order_label)
                        continue

                    plan_total = len(plans)
                    log.info("[%s/%s] %s 共 %s 期计划", idx, total_orders, order_label, plan_total)

                    # 统计各期状态
                    repaid_count = sum(1 for p in plans if p.get("state") == 2)
                    overdue_count = sum(1 for p in plans if p.get("state") == 3)
                    skip_count += repaid_count
                    order_skip += repaid_count
                    total_plans += plan_total
                    log.info(
                        "  已还=%s 期  逾期=%s 期  其他=%s 期",
                        repaid_count, overdue_count, plan_total - repaid_count - overdue_count,
                    )

                    picked = pick_deductible_plan_row(plans)
                    if picked is None:
                        log.info(
                            "[%s/%s] %s 无可划扣期次（已无逾期或未到还款日），跳过本单",
                            idx, total_orders, order_label,
                        )
                        if per_customer_first and order_row_is_overdue_list(order):
                            log.info("[%s/%s] 本单无可划扣期次（仍计为该客户首笔逾期订单）", idx, total_orders)
                        continue

                    plan_idx, plan = picked
                    log.info(
                        "  canSaveId 命中: 期次=%s plan行id=%s stateText=%s deductResult=%s 还款日=%s",
                        plan.get("repaymentNo"),
                        plan.get("id"),
                        plan.get("stateText"),
                        plan.get("deductResult"),
                        plan.get("repaymentDate"),
                    )

                    attempted_plan_ids: set[int] = set()
                    queue: list[tuple[int, dict[str, Any]]] = [picked]
                    while queue:
                        if stop.is_set():
                            log.info("  %s 用户中断，停止本单继续尝试", order_label)
                            break
                        if (order_success + order_fail) >= max_attempts_per_order:
                            log.info(
                                "  %s 已达本单最大尝试次数 %s，停止",
                                order_label, max_attempts_per_order,
                            )
                            break
                        plan_idx, plan = queue.pop(0)
                        plan_row_id = int(plan.get("id"))
                        if plan_row_id in attempted_plan_ids:
                            continue
                        attempted_plan_ids.add(plan_row_id)

                        rec: dict[str, Any] = {
                            "time": datetime.now(timezone.utc).astimezone().isoformat(),
                            "action": "single_deduct",
                            "endpoint": DEDUCT_PATH,
                            "plan_row_id": plan_row_id,
                            "dry_run": False,
                            "base": base,
                            "order_id": int(oid),
                            "credit_order": str(credit_order),
                            "repayment_no": plan.get("repaymentNo"),
                            "customer_name": order.get("customerName") or order.get("realName"),
                            "customer_phone": order.get("customerPhone"),
                            "product_name": order.get("productName"),
                        }
                        deduct_amount = _deduct_amount_from_plan(plan)
                        if deduct_amount is not None:
                            rec["repay_amount"] = deduct_amount
                        try:
                            res = single_deduct(s, base, plan_row_id, False, repay_amount=deduct_amount)
                            rec["result"] = res
                            rec["ok"] = bool(res.get("code") == 200)
                            if rec["ok"]:
                                success_count += 1
                                order_success += 1
                                product_stats.setdefault(order.get("productName") or "?", {"ok": 0, "fail": 0, "skip": 0})["ok"] += 1
                                log.info(
                                    "  %s 期次=%s plan行id=%s: 划扣成功",
                                    order_label,
                                    plan.get("repaymentNo"),
                                    plan_row_id,
                                )
                                if per_customer_first:
                                    log.info("  %s 已成功发起一笔划扣，继续下一客户/订单", order_label)
                                    append_deduct_audit(audit_path, rec)
                                    deduct_attempts += 1
                                    break
                            else:
                                fail_count += 1
                                order_fail += 1
                                product_stats.setdefault(order.get("productName") or "?", {"ok": 0, "fail": 0, "skip": 0})["fail"] += 1
                                log.warning(
                                    "  %s 期次=%s plan行id=%s: 划扣失败 %s",
                                    order_label,
                                    plan.get("repaymentNo"),
                                    plan_row_id,
                                    res,
                                )
                                target_no = parse_deduct_start_repayment_no(res.get("msg"))
                                if target_no is not None:
                                    target_item = find_plan_by_repayment_no(plans, target_no)
                                    if target_item is not None:
                                        tid = int(target_item[1].get("id"))
                                        if tid not in attempted_plan_ids:
                                            log.info("  %s 失败提示发起期数[%s]，切换对应期次重试", order_label, target_no)
                                            queue.insert(0, target_item)
                                msg_text = str(res.get("msg") or "")
                                hard_stop_hints = (
                                    "该时段不允许还款",
                                    "当前订单存在还款中订单",
                                    "商户号未配置成功",
                                )
                                if any(h in msg_text for h in hard_stop_hints):
                                    log.info("  %s 业务提示不可继续重试（%s），结束本单尝试", order_label, msg_text)
                                    append_deduct_audit(audit_path, rec)
                                    deduct_attempts += 1
                                    break
                        except Exception as e:
                            rec["ok"] = False
                            rec["error"] = repr(e)
                            fail_count += 1
                            order_fail += 1
                            log.error(
                                "  %s 期次=%s plan行id=%s: 请求异常 %s",
                                order_label,
                                plan.get("repaymentNo"),
                                plan_row_id,
                                e,
                            )
                        append_deduct_audit(audit_path, rec)
                        deduct_attempts += 1
                        # 划扣间延迟：避免触发后端频率限制
                        inter_delay = float(os.environ.get("AYIDAI_DEDUCT_INTER_DELAY", "0.5"))
                        if inter_delay > 0:
                            time.sleep(inter_delay)
                        if per_customer_first and order_success > 0:
                            break

                    log.info(
                        "[%s/%s] %s 本单完成: 成功=%s 跳过=%s 失败=%s",
                        idx, total_orders, order_label,
                        order_success, order_skip, order_fail,
                    )
                    if per_customer_first and order_success == 0 and order_fail == 0 and order_row_is_overdue_list(order):
                        log.info("[%s/%s] %s 无可划扣期次（仍计为该客户首笔逾期订单）", idx, total_orders, order_label)

                log.info("=" * 40)
                log.info("===== 汇总 =====")
                log.info(
                    "订单: %s 笔 | 计划共: %s 期 | 成功: %s | 跳过: %s | 失败: %s | 无望跳过: %s",
                    total_orders, total_plans, success_count, skip_count, fail_count, hopeless_count,
                )
                if per_customer_first:
                    log.info("按客户首笔逾期单模式：共处理 %s 个不同客户/户", len(customers_touched))
                if product_stats:
                    log.info("--- 按产品 ---")
                    for pn in sorted(product_stats.keys()):
                        s = product_stats[pn]
                        log.info("  %s: 成功=%s  失败=%s  无望跳过=%s", pn, s["ok"], s["fail"], s["skip"])
                if deduct_attempts == 0:
                    log.warning("未发起任何划扣（可能没有符合条件的逾期单或期次）。")
                summary = (
                    f"订单: {total_orders} 笔\n计划: {total_plans} 期\n成功: {success_count}\n跳过: {skip_count}\n失败: {fail_count}\n无望跳过: {hopeless_count}"
                )
                if per_customer_first:
                    summary += f"\n（每户首笔逾期单各划一期，共 {len(customers_touched)} 户）"
                if product_stats:
                    summary += "\n\n按产品:\n"
                    for pn in sorted(product_stats.keys()):
                        s = product_stats[pn]
                        summary += f"\n{pn}: 成功{s['ok']} 失败{s['fail']} 跳过{s['skip']}"
                self.root.after(0, lambda: messagebox.showinfo("完成", summary))
            except Exception as e:
                log.exception("一键处理失败")
                self.root.after(0, lambda: messagebox.showerror("失败", str(e)))
            finally:
                def restore_ui() -> None:
                    self.stop_btn.grid_remove()
                    self.start_btn.grid()
                    self.start_btn.configure(state=tk.NORMAL, text="开始一键处理")
                self.root.after(0, restore_ui)

        self.logger.info("===== 开始一键处理 =====")
        self._run_in_thread(work)


def run_gui() -> None:
    root = tk.Tk()
    AyidaiGuiApp(root)
    root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description="安易代委外列表 / 划扣（无参数时直接启动 GUI）")
    parser.add_argument("--base", default=os.environ.get("AYIDAI_BASE", DEFAULT_BASE), help="API 根 URL")
    parser.add_argument(
        "--cookie",
        default=os.environ.get("AYIDAI_COOKIE", ""),
        help="可选，浏览器复制的整段 Cookie；可含 Admin-Token=。也可用 @路径 从文件读取",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fetch = sub.add_parser("fetch-all", help="按筛选 JSON 分页拉齐列表")
    p_fetch.add_argument("--filter", required=True, help="与浏览器请求体一致的 JSON 文件")
    p_fetch.add_argument("--out", default="orders_out.json")
    p_fetch.add_argument("--page-size", type=int, default=50)
    p_fetch.set_defaults(func=cmd_fetch_all)

    p_plan = sub.add_parser("plan", help="打印某订单还款计划（含每期 id，供划扣）")
    p_plan.add_argument("--order-id", type=int, required=True)
    p_plan.add_argument("--credit-order", required=True, help="列表里的 creditOrder 字段")
    p_plan.add_argument("--size", type=int, default=100)
    p_plan.add_argument("--out", help="可选，写入 JSON")
    p_plan.set_defaults(func=cmd_plan)

    p_deduct = sub.add_parser("deduct", help="对还款计划某一期发起划扣（GET single/deduct?id=）")
    p_deduct.add_argument("--plan-id", type=int, required=True, help="getOrderPlan 返回的该期 id")
    p_deduct.add_argument("--dry-run", action="store_true")
    p_deduct.add_argument(
        "--order-id",
        type=int,
        default=None,
        help="可选，仅写入审计日志便于对账",
    )
    p_deduct.add_argument(
        "--credit-order",
        default="",
        help="可选，仅写入审计日志",
    )
    p_deduct.add_argument(
        "--audit-log",
        default=os.environ.get("AYIDAI_DEDUCT_LOG") or default_deduct_audit_log(),
        help="代扣审计日志路径（每行一条 JSON）；默认在可执行文件/脚本同目录 deduct_audit.log；环境变量 AYIDAI_DEDUCT_LOG 可覆盖",
    )
    p_deduct.add_argument(
        "--no-audit-log",
        action="store_true",
        help="不写代扣审计文件",
    )
    p_deduct.add_argument(
        "--repay-amount",
        type=float,
        default=None,
        help="划扣金额（元）。前端 canRepayAmount 有值时需传入；一般等于 remainTotalAmount",
    )
    p_deduct.set_defaults(func=cmd_deduct)

    p_test = sub.add_parser("test-one", help="测试单个订单：拉取计划 → 显示可划扣期次 → 确认后发起划扣")
    p_test.add_argument("--order-id", type=int, required=True, help="列表接口中的 orderId（不是 creditOrder）")
    p_test.add_argument("--credit-order", required=True, help="列表中的 creditOrder 字段")
    p_test.add_argument("--customer-id", default="", help="可选")
    p_test.add_argument("--dry-run", action="store_true", help="仅显示会做什么，不实际发起")
    p_test.add_argument("--yes", action="store_true", help="跳过确认，直接发起划扣")
    p_test.set_defaults(func=cmd_test_one)

    p_gui = sub.add_parser("gui", help="简单窗口：选择筛选 JSON 并拉全量列表")

    def cmd_gui(_a: argparse.Namespace, _log: logging.Logger) -> int:
        run_gui()
        return 0

    p_gui.set_defaults(func=cmd_gui)

    args = parser.parse_args()
    args.cookie = load_cookie_arg(getattr(args, "cookie", "") or "")
    if args.cmd == "gui":
        run_gui()
        return 0
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log = logging.getLogger("cli")
    return args.func(args, log)


if __name__ == "__main__":
    # 双击/无参数启动：仅打开图形界面（无控制台输出依赖）
    if len(sys.argv) == 1:
        run_gui()
        sys.exit(0)
    sys.exit(main())
