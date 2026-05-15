# PyInstaller：无控制台（windowed）。Mac/Linux 产出 dist/AyidaiTool；Windows 产出 dist/AyidaiTool.exe
# 必须在目标系统上构建：不能在 Mac 上交叉编译出 .exe，请在 Windows 上运行 build_dist.bat
# 构建：pip install pyinstaller requests && pyinstaller AyidaiTool.spec

a = Analysis(
    ["ayidai_automation.py"],
    pathex=[],
    binaries=[],
    datas=[("filter.example.json", ".")],
    hiddenimports=["requests", "urllib3", "appdirs"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 我们不依赖 pkg_resources/setuptools；排除可避免 pyi_rth_pkgres 在 onefile 解压目录里解析版本失败
    excludes=["pkg_resources", "setuptools"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="AyidaiTool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AyidaiTool",
)
