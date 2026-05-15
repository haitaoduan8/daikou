@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/2] 安装/更新 PyInstaller 与 requests（需已安装 Python 3.9+ 并加入 PATH）...
REM 可选：set PIP_USE_CN_MIRROR=1 使用清华镜像（与 %%APPDATA%%\pip\pip.ini 二选一）
if "%PIP_USE_CN_MIRROR%"=="1" (
  python -m pip install -U pip --timeout 120 -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
  python -m pip install "pyinstaller>=6.0" "requests>=2.28" --timeout 120 -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
) else (
  python -m pip install -U pip --timeout 120
  python -m pip install "pyinstaller>=6.0" "requests>=2.28" --timeout 120
)
if errorlevel 1 (
  echo pip 失败，请检查网络或改用国内镜像后再试。
  pause
  exit /b 1
)

echo [2/2] 打包...
pyinstaller --noconfirm --clean AyidaiTool.spec
if errorlevel 1 (
  echo PyInstaller 失败。
  pause
  exit /b 1
)

echo.
echo 完成。请在 dist 目录查看：AyidaiTool.exe（双击无黑框，日志在程序内）
echo 将 AyidaiTool.exe 单独拷贝分发即可；同目录会生成 orders_out.json、deduct_audit.log 等。
pause
