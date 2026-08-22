@echo off
REM 本脚本文件以 UTF-8 BOM 编码保存，并在首行切换控制台到 UTF-8(65001)，
REM 保证中文 Windows CMD 下进度文案正常显示、且不会被误解析为命令片段。
>nul chcp 65001
setlocal EnableExtensions EnableDelayedExpansion

REM --- 自动定位：始终以脚本所在目录作为工作目录 ---
set "SCRIPT_DIR=%~dp0"
if "!SCRIPT_DIR:~-1!"=="\" set "SCRIPT_DIR=!SCRIPT_DIR:~0,-1!"
cd /d "!SCRIPT_DIR!"
if !ERRORLEVEL! NEQ 0 (
    echo [错误] 无法切换到脚本所在目录："!SCRIPT_DIR!"
    echo        请确认路径可访问（是否为网络盘 UNC 路径？）。
    call :PAUSE_IF_INTERACTIVE
    exit /b 1
)

set "APP_NAME=WxReadAssistant"
set "ENTRY=main.py"
set "HELPER=_build_helper.py"
set "DIST_DIR=dist"
set "OUT_EXE=!DIST_DIR!\!APP_NAME!\!APP_NAME!.exe"
set "BUILD_LOG=build.log"
set "PYI_LOG=pyinstaller.log"
set "RC_LOG=build_rc.txt"

echo [进度] 当前工作目录："!CD!"
where python >nul 2>nul
if !ERRORLEVEL! NEQ 0 (
    echo [错误] 未在 PATH 中检测到 Python 解释器。
    echo        请先安装 Python 3.10+ 并加入 PATH，或先激活虚拟环境 venv。
    call :PAUSE_IF_INTERACTIVE
    exit /b 1
)
for /f "delims=" %%i in ('python -c "import sys; sys.stdout.write(sys.executable)"') do set "PY_EXE=%%i"
echo [进度] 使用 Python 解释器："!PY_EXE!"

REM --- 检查入口脚本 + 构建辅助脚本是否存在 ---
if not exist "!ENTRY!" (
    echo [错误] 未找到入口脚本："!CD!\!ENTRY!"
    echo        请确认在项目根目录运行本脚本。
    call :PAUSE_IF_INTERACTIVE
    exit /b 1
)
if not exist "!HELPER!" (
    echo [错误] 未找到构建辅助脚本："!CD!\!HELPER!"
    echo        该文件应与 build.bat 位于同一目录。
    call :PAUSE_IF_INTERACTIVE
    exit /b 1
)

REM --- 检查并安装 PyInstaller 到当前 Python 环境 ---
python -c "import PyInstaller, sys; sys.stdout.write(PyInstaller.__version__); sys.stdout.flush()" >nul 2>nul
if !ERRORLEVEL! NEQ 0 (
    echo [警告] 当前 Python 环境中未检测到 PyInstaller。
    echo [进度] 正在通过 pip 安装 PyInstaller（可能需要几分钟，请勿关闭窗口）...
    python -m pip install --upgrade "pyinstaller>=6.0"
    if !ERRORLEVEL! NEQ 0 (
        echo [错误] PyInstaller 安装失败。
        echo        请手动执行：python -m pip install pyinstaller
        call :PAUSE_IF_INTERACTIVE
        exit /b 1
    )
)

echo [进度] 正在清理上次构建的临时产物（build / dist / 旧日志）...
if exist "build"                 rd /s /q "build" >nul 2>&1
if exist "!DIST_DIR!\!APP_NAME!" rd /s /q "!DIST_DIR!\!APP_NAME!" >nul 2>&1
if exist "!PYI_LOG!"             del /f /q "!PYI_LOG!" >nul 2>&1
if exist "!BUILD_LOG!"           del /f /q "!BUILD_LOG!" >nul 2>&1
if exist "!RC_LOG!"              del /f /q "!RC_LOG!" >nul 2>&1

REM --- 外层构建日志（与 PyInstaller 细节日志分开，便于快速排查） ---
(
  echo ---------------------------------------------------------------
  echo [构建开始] !date! !time!
  echo [解释器  ] !PY_EXE!
  echo [入口文件] !CD!\!ENTRY!
  echo [辅助脚本] !CD!\!HELPER!
  echo ---------------------------------------------------------------
) ^> "!BUILD_LOG!"

echo [进度] 正在启动 PyInstaller 打包（通过辅助脚本执行，详细输出见 "!PYI_LOG!"）...

REM --- 通过环境变量传递参数给辅助脚本（setlocal 内变量会自动被子进程继承） ---
set "APP_NAME=!APP_NAME!"
set "ENTRY=!ENTRY!"
set "PYI_LOG=!PYI_LOG!"
set "RC_FILE=!RC_LOG!"

python "!HELPER!"
set "HELPER_RC=!ERRORLEVEL!"

REM --- 解析实际构建返回码 ---
set "BUILD_RC=9009"
if exist "!RC_LOG!" (
    for /f "usebackq delims=" %%v in ("!RC_LOG!") do set "BUILD_RC=%%v"
)
REM 去除前后引号和首尾空格（安全兜底，辅助脚本实际不会产出这些）。
set "BUILD_RC=!BUILD_RC:"=!"
for /f "tokens=* delims= " %%a in ("!BUILD_RC!") do set "BUILD_RC=%%a"
REM 如果 RC_LOG 不存在 / 为空 / 非数字，回退到辅助脚本退出码。
if "!BUILD_RC!"=="" set "BUILD_RC=!HELPER_RC!"
set "_IS_NUM=1"
for /l %%i in (0,1,9) do set "BUILD_RC=!BUILD_RC:%%i=!"
if not "!BUILD_RC!"=="" set "_IS_NUM=0"
if "!_IS_NUM!"=="0" (
    REM BUILD_RC 含非数字字符：再一次从 RC_LOG 读取，仍无效则使用 helper 退出码。
    set "BUILD_RC=!HELPER_RC!"
) else (
    REM BUILD_RC 被"逐位 0-9 替换后变空" → 原始值全为数字，重新从 RC_LOG / fallback 读取。
    set "BUILD_RC=9009"
    if exist "!RC_LOG!" (
        for /f "usebackq delims=" %%v in ("!RC_LOG!") do set "BUILD_RC=%%v"
    )
    if "!BUILD_RC!"=="9009" set "BUILD_RC=!HELPER_RC!"
)
set "_IS_NUM="

(
  echo ---------------------------------------------------------------
  echo [构建结束] 辅助脚本退出码 = !HELPER_RC!   PyInstaller 退出码 = !BUILD_RC!
  echo ---------------------------------------------------------------
) ^>^> "!BUILD_LOG!"

if !BUILD_RC! NEQ 0 (
    echo [错误] 构建失败，PyInstaller 返回码：!BUILD_RC!（辅助脚本返回码=!HELPER_RC!）。
    echo        PyInstaller 日志："!CD!\!PYI_LOG!"
    echo        构建外层日志  ："!CD!\!BUILD_LOG!"
    echo        PyInstaller 日志的最后 60 行如下：
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "if (Test-Path -LiteralPath '!PYI_LOG!') { Get-Content -LiteralPath '!PYI_LOG!' -Tail 60 }"
    call :PAUSE_IF_INTERACTIVE
    exit /b !BUILD_RC!
)

REM --- 硬性校验：打包报告成功后必须实际生成 EXE ---
if not exist "!OUT_EXE!" (
    echo [错误] PyInstaller 报告成功，但未在预期位置生成 EXE："!OUT_EXE!"
    echo        PyInstaller 日志："!CD!\!PYI_LOG!"
    echo        构建外层日志  ："!CD!\!BUILD_LOG!"
    call :PAUSE_IF_INTERACTIVE
    exit /b 1
)

echo [进度] 构建成功 ✅  输出目录："!DIST_DIR!\!APP_NAME!"
echo [进度] 可执行文件    ："!OUT_EXE!"
(
  echo [构建完成] !OUT_EXE!
) ^>^> "!BUILD_LOG!"
echo.
echo        请直接双击上面的 .exe 文件启动软件。
endlocal

call :PAUSE_IF_INTERACTIVE
exit /b 0

REM ============================================================
REM  辅助：仅在用户"双击运行"时暂停，方便查看结果。
REM  在 CI / 自动化脚本中调用时不暂停。
REM  环境变量 NO_PAUSE=1 时强制不暂停。
REM ============================================================
:PAUSE_IF_INTERACTIVE
if /i "!NO_PAUSE!"=="1" exit /b 0
set "CL=%CMDCMDLINE%"
echo "!CL!" | findstr /i /r /c:" /c " /c:" -c " >nul
if !ERRORLEVEL! EQU 0 (
    echo.
    pause
)
exit /b 0