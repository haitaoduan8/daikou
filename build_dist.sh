#!/usr/bin/env bash
# macOS / Linux 打包（产出本机可执行文件，不是 .exe）。Windows 请用 build_dist.bat
# 可选：export PIP_USE_CN_MIRROR=1 使用清华 PyPI 镜像（与 ~/.pip/pip.conf 二选一即可）
set -euo pipefail
cd "$(dirname "$0")"
if [ "${PIP_USE_CN_MIRROR:-}" = "1" ]; then
  CN_MIRROR=( -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn )
else
  CN_MIRROR=()
fi
if ! python3 -m PyInstaller --version >/dev/null 2>&1; then
  echo "正在安装 PyInstaller…"
  python3 -m pip install "${CN_MIRROR[@]}" --timeout 120 "pyinstaller>=6.0" "requests>=2.28"
fi
pyinstaller --noconfirm --clean AyidaiTool.spec
echo "完成: $(pwd)/dist/AyidaiTool"
echo "说明: .exe 必须在 Windows 上执行 build_dist.bat 生成，无法在本机构建 Windows 安装包。"
