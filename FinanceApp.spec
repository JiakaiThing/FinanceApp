# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# Bundle the FA logo so the PyInstaller exe can find it via
# ``sys._MEIPASS`` at runtime (see ``_project_root`` in main_window).
datas = [
    ('FALogo.ico', '.'),
    ('FALogo.png', '.'),
]
binaries = []
hiddenimports = []
tmp_ret = collect_all('matplotlib')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# tkinterdnd2 ships a native ``tkdnd`` Tcl extension that must be bundled
# alongside the Python package, or drag-and-drop fails to initialise at
# runtime. collect_all grabs the platform tkdnd binaries + Tcl scripts.
tmp_ret = collect_all('tkinterdnd2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='FinanceApp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Embedded into the .exe's Win32 resources so File Explorer
    # shows the FA logo on the binary itself.
    icon='FALogo.ico',
)
