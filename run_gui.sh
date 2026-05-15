#!/usr/bin/env bash
# 在本机图形界面下启动 GUI。
# 勿用苹果自带的 /usr/bin/python3（3.9）：在 macOS 26+ 上 Tk 会 abort。
# 安装：brew install python@3.12 python-tk@3.12
# 若 pip 报 pyexpat / libexpat 符号缺失：brew install expat（脚本会优先加载 Homebrew 的 libexpat）
# 可选：export AYIDAI_PYTHON=/opt/homebrew/bin/python3.12
set -euo pipefail
cd "$(dirname "$0")"

# 是否为苹果 Command Line Tools / 系统自带的 python3（Tk 易崩溃）
apple_system_python() {
  case "$1" in
    /usr/bin/python3 | /Library/Developer/CommandLineTools/usr/bin/python3)
      return 0
      ;;
  esac
  return 1
}

pick_python() {
  if [ -n "${AYIDAI_PYTHON:-}" ]; then
    printf '%s' "${AYIDAI_PYTHON}"
    return
  fi

  local base p c v

  # 1) Homebrew 各版本 keg：$(brew --prefix python@3.12)/bin/python3.12
  if command -v brew >/dev/null 2>&1; then
    for v in 3.13 3.12 3.11; do
      base=$(brew --prefix "python@${v}" 2>/dev/null) || continue
      p="${base}/bin/python${v}"
      if [ -x "$p" ]; then
        printf '%s' "$p"
        return
      fi
    done
    # brew install python（无 @ 版本号）→ 常为最新 3.x
    base=$(brew --prefix python 2>/dev/null) || true
    if [ -n "${base:-}" ] && [ -x "${base}/bin/python3" ]; then
      printf '%s' "${base}/bin/python3"
      return
    fi
    hb=$(brew --prefix 2>/dev/null) || true
    if [ -n "${hb:-}" ]; then
      for p in "${hb}/bin/python3.13" "${hb}/bin/python3.12" "${hb}/bin/python3.11" "${hb}/bin/python3"; do
        if [ -x "$p" ] && ! apple_system_python "$p"; then
          printf '%s' "$p"
          return
        fi
      done
    fi
  fi

  # 2) 常见路径（不依赖 brew 命令）
  for p in \
    /opt/homebrew/bin/python3.13 \
    /opt/homebrew/bin/python3.12 \
    /opt/homebrew/bin/python3.11 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3.13 \
    /usr/local/bin/python3.12 \
    /usr/local/bin/python3.11 \
    /usr/local/bin/python3; do
    if [ -x "$p" ] && ! apple_system_python "$p"; then
      printf '%s' "$p"
      return
    fi
  done

  # 3) PATH 里的 python3.13 / 3.12 / 3.11（跳过 /usr/bin）
  for c in python3.13 python3.12 python3.11; do
    p=$(command -v "$c" 2>/dev/null) || continue
    case "$p" in
      /usr/bin/*) continue ;;
    esac
    if [ -x "$p" ]; then
      printf '%s' "$p"
      return
    fi
  done

  command -v python3
}

# Homebrew Python 有时会链到系统旧版 libexpat，导致 pip 在加载 pyexpat 时崩溃
ensure_homebrew_expat_dyld() {
  [[ "$(uname -s)" == Darwin ]] || return 0
  case "${PY:-}" in
    /opt/homebrew/* | /usr/local/*) ;;
    *) return 0 ;;
  esac
  local d
  for d in /opt/homebrew/opt/expat/lib /usr/local/opt/expat/lib; do
    if [ -e "$d/libexpat.1.dylib" ] || [ -e "$d/libexpat.dylib" ]; then
      export DYLD_LIBRARY_PATH="$d${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
      return 0
    fi
  done
}

fail_install_hint() {
  echo "" >&2
  echo "当前选用的 Python: $PY" >&2
  if apple_system_python "$PY"; then
    echo "这是苹果自带的 Python 3.9，在较新 macOS 上 Tk 会崩溃，本脚本不会再用它做测试。" >&2
  else
    echo "Tk 自检未通过（图形库无法初始化）。" >&2
  fi
  echo "" >&2
  echo "请安装 Homebrew Python 与 Tk（任选其一版本）：" >&2
  echo "  brew install python@3.12 python-tk@3.12" >&2
  echo "  # 或: brew install python" >&2
  echo "若 pip 报 pyexpat / libexpat 相关错误：brew install expat" >&2
  echo "" >&2
  echo "安装后在本终端执行（Apple Silicon 常见路径如下，装好后可用 \`which python3.12\` 确认）：" >&2
  echo "  export AYIDAI_PYTHON=\"\$(command -v python3.12)\"" >&2
  echo "  ./run_gui.sh" >&2
  echo "" >&2
  echo "若已安装仍提示找不到，请把下面两条命令的输出贴出来：" >&2
  echo "  which -a python3.12" >&2
  echo "  brew --prefix python@3.12 && ls \"\$(brew --prefix python@3.12)/bin\"" >&2
  echo "" >&2
}

PY="$(pick_python)"
ensure_homebrew_expat_dyld

if apple_system_python "$PY"; then
  fail_install_hint
  exit 1
fi

if ! (
  "$PY" -c "import tkinter as tk; r=tk.Tk(); r.withdraw(); r.destroy()" 2>/dev/null
); then
  fail_install_hint
  exit 1
fi

# Homebrew Python 启用 PEP 668，不向系统 site-packages 写包；用项目内 venv
VENV="${PWD}/.venv"
if [ ! -x "${VENV}/bin/python" ]; then
  "$PY" -m venv "${VENV}"
  "${VENV}/bin/python" -m pip install -U pip
fi
VPY="${VENV}/bin/python"
"$VPY" -m pip install -r requirements.txt
exec "$VPY" ayidai_automation.py
