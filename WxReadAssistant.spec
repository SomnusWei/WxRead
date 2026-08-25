# -*- mode: python ; coding: utf-8 -*-
r"""WxReadAssistant v2.0 打包配置。

用法：
  cd e:\item\wxread\v2
  python -m PyInstaller WxReadAssistant.spec --noconfirm
"""
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

datas = []
binaries = []
hiddenimports = [
    # PySide6 核心 + 嵌入式浏览器（CDP 扫码登录需要 QWebEngineView）
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'PySide6.QtNetwork',
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets',
    'PySide6.QtPrintSupport',
    # CDP / HTTP 依赖（websocket-client 新版结构）
    'websocket',
    'websocket._abnf',
    'websocket._app',
    'websocket._core',
    'websocket._exceptions',
    'websocket._http',
    'websocket._logging',
    'websocket._socket',
    'websocket._ssl_compat',
    'websocket._url',
    'websocket._utils',
    'websocket._cookiejar',
    'websocket._dispatcher',
    'websocket._handshake',
    'websocket._wsdump',
    # HTTP 栈
    'requests',
    'urllib3',
    'certifi',
    'charset_normalizer',
    'idna',
]

# PySide6 数据/动态库/子模块收集（含 WebEngine，供 CDP 扫码登录使用）
for mod in (
    'PySide6',
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'PySide6.QtNetwork',
    'PySide6.QtWebEngineCore',
    'PySide6.QtWebEngineWidgets',
    'PySide6.QtPrintSupport',
):
    try:
        datas += collect_data_files(mod)
        binaries += collect_dynamic_libs(mod)
        hiddenimports += collect_submodules(mod)
    except Exception:
        pass


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 仍排除 QML/Quick 等 v2 不用的模块（QtWebEngine 已加回）
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtQuick3D',
        'PySide6.QtCharts',
        'PySide6.QtDataVisualization',
        'PySide6.Qt3DCore',
    ],
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
