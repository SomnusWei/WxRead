# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_dynamic_libs
from PyInstaller.utils.hooks import collect_submodules

datas = []
binaries = []
hiddenimports = [
    'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets',
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
    'websocket', 'websocket._abnf', 'websocket._app', 'websocket._core', 'websocket._exceptions',
    'websocket._http', 'websocket._logging', 'websocket._socket', 'websocket._ssl', 'websocket._url',
    'websocket._utils', 'requests', 'urllib3', 'certifi', 'charset_normalizer', 'idna',
]
datas += collect_data_files('PySide6.QtCore')
datas += collect_data_files('PySide6.QtGui')
datas += collect_data_files('PySide6.QtWidgets')
datas += collect_data_files('PySide6.QtWebEngineCore')
datas += collect_data_files('PySide6.QtWebEngineWidgets')
binaries += collect_dynamic_libs('PySide6.QtCore')
binaries += collect_dynamic_libs('PySide6.QtGui')
binaries += collect_dynamic_libs('PySide6.QtWidgets')
binaries += collect_dynamic_libs('PySide6.QtWebEngineCore')
binaries += collect_dynamic_libs('PySide6.QtWebEngineWidgets')
hiddenimports += collect_submodules('PySide6.QtCore')
hiddenimports += collect_submodules('PySide6.QtGui')
hiddenimports += collect_submodules('PySide6.QtWidgets')
hiddenimports += collect_submodules('PySide6.QtWebEngineCore')
hiddenimports += collect_submodules('PySide6.QtWebEngineWidgets')

a = Analysis(
    ['cdp_test_standalone.py'],
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
    name='CDPStandaloneTest',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 显示控制台，方便看日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app/resources/app.ico',
)
