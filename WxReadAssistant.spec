# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

datas = []
binaries = []
hiddenimports = ['PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'PySide6.QtNetwork', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtPrintSupport']
datas += collect_data_files('PySide6.QtCore')
datas += collect_data_files('PySide6.QtGui')
datas += collect_data_files('PySide6.QtWidgets')
datas += collect_data_files('PySide6.QtNetwork')
datas += collect_data_files('PySide6.QtWebEngineCore')
datas += collect_data_files('PySide6.QtWebEngineWidgets')
datas += collect_data_files('PySide6.QtPrintSupport')
binaries += collect_dynamic_libs('PySide6.QtCore')
binaries += collect_dynamic_libs('PySide6.QtGui')
binaries += collect_dynamic_libs('PySide6.QtWidgets')
binaries += collect_dynamic_libs('PySide6.QtNetwork')
binaries += collect_dynamic_libs('PySide6.QtWebEngineCore')
binaries += collect_dynamic_libs('PySide6.QtWebEngineWidgets')
binaries += collect_dynamic_libs('PySide6.QtPrintSupport')
hiddenimports += collect_submodules('PySide6.QtCore')
hiddenimports += collect_submodules('PySide6.QtGui')
hiddenimports += collect_submodules('PySide6.QtWidgets')
hiddenimports += collect_submodules('PySide6.QtNetwork')
hiddenimports += collect_submodules('PySide6.QtWebEngineCore')
hiddenimports += collect_submodules('PySide6.QtWebEngineWidgets')
hiddenimports += collect_submodules('PySide6.QtPrintSupport')


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
    [],
    exclude_binaries=True,
    name='WxReadAssistant',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WxReadAssistant',
)
