# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('musicdl')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_zhconv = collect_all('zhconv')
datas += tmp_zhconv[0]; binaries += tmp_zhconv[1]; hiddenimports += tmp_zhconv[2]



# 结构性瘦身：剔除不必要的庞大外部依赖
excluded_modules = [
    'av', 'numpy', 'scipy', 'matplotlib', 'pandas',
    'tkinter', '_tkinter', 'tcl', 'tk',
    'unittest', 'test', 'pydoc', 'doctest',
    'PIL', 'Pillow', 'lxml',
]

a = Analysis(
    ['musicdownload.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    noarchive=False,
    optimize=1,
)

# 过滤掉 PyQt5 冗余的 20MB 软件 OpenGL 驱动 opengl32sw.dll
a.binaries = [x for x in a.binaries if not x[0].lower().endswith('opengl32sw.dll')]

# 过滤掉冗余的多国语言文件
a.datas = [x for x in a.datas if not any(k in x[0].lower() for k in ['emoji_ru', 'emoji_ar', 'emoji_fa', 'emoji_es', 'emoji_it', 'emoji_de', 'emoji_pt'])]

pyz = PYZ(a.pure)

# ==================== UPX 白名单防崩溃清单 ====================
upx_protect_list = [
    'qwindows.dll',       # Qt Windows 核心平台插件
    'vcruntime140.dll',   # VC 运行时核心
    'vcruntime140_1.dll', # VC 运行时拓展
    'msvcp140.dll',       # C++ 标准库
    'ucrtbase.dll',       # 通用 C 运行时
]

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='musicdownload',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=upx_protect_list,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
