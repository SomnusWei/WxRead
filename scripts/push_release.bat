@echo off
REM =============================================================
REM  WxReadAssistant 一键发布推送脚本
REM  功能：
REM    ① 自动探测本机可用 HTTPS 代理（7890/7897/1080/10809/8080/3128）
REM       优先复用当前 Git 全局代理；否则自动扫描端口
REM    ② 强制 Windows SChannel TLS 后端（兼容性最好）
REM    ③ 推送 main 到 origin
REM    ④ 如果不存在 vX.Y.Z Tag 则自动创建并推送
REM  用法：
REM    双击运行，或在 PowerShell/cmd 中：
REM      cd /d e:\item\wxread
REM      scripts\push_release.bat
REM  环境变量（可预先设置）：
REM    WX_PUSH_TAG     指定要推送的版本 Tag，如 "v2.2.0" （留空则从 icon_store.py 自动解析）
REM    WX_PUSH_PROXY   强制指定代理 URL，如 "http://127.0.0.1:7890"
REM                    （留空则自动探测；设为 "direct" 表示直连不使用代理）
REM =============================================================
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0.."
echo ============ WxReadAssistant push_release.bat ============
echo.

REM ==== 1. 从 app/ui/icon_store.py 中解析 APP_VERSION 作为默认 Tag ====
if "%WX_PUSH_TAG%"=="" (
  for /f "usebackq tokens=2 delims= " %%v in (`findstr /r /c:"^APP_VERSION\s*=" "app\ui\icon_store.py"`) do (
    for /f "usebackq tokens=2 delims==" %%i in (`echo %%v`) do set "VER_RAW=%%i"
  )
  REM 去引号和空格
  setlocal EnableDelayedExpansion
  set "VER_RAW=!VER_RAW:"=!"
  for /f "tokens=* delims= " %%a in ("!VER_RAW!") do set "VER_TRIM=%%a"
  endlocal & set "APP_VER=%VER_TRIM%"
  if NOT "%APP_VER%"=="" set "WX_PUSH_TAG=v%APP_VER%"
)
if "%WX_PUSH_TAG%"=="" (
  echo [WARN] 未能从 icon_store.py 解析版本号，请 set WX_PUSH_TAG=v2.2.0 后重试。
  set "WX_PUSH_TAG=v2.2.0"
)
echo [1/6] 目标 Tag      = %WX_PUSH_TAG%
echo [1/6] 当前 commit   =
git log -1 --oneline

REM ==== 2. 探测代理 ====
set "PROXY_URL="
if /i "%WX_PUSH_PROXY%"=="direct" (
  echo [2/6] WX_PUSH_PROXY=direct，走直连（不设代理）
  goto :proxy_done
)
if NOT "%WX_PUSH_PROXY%"=="" (
  set "PROXY_URL=%WX_PUSH_PROXY%"
  echo [2/6] 使用显式代理 WX_PUSH_PROXY=%PROXY_URL%
  goto :proxy_done
)

REM 先用 git 全局代理
for /f "usebackq delims=" %%p in (`git config --global --get https.proxy 2^>nul`) do set "PROXY_URL=%%p"
if NOT "%PROXY_URL%"=="" (
  echo [2/6] 使用 Git 全局 https.proxy = %PROXY_URL%
  goto :proxy_done
)
for /f "usebackq delims=" %%p in (`git config --global --get http.proxy 2^>nul`) do set "PROXY_URL=%%p"
if NOT "%PROXY_URL%"=="" (
  echo [2/6] 使用 Git 全局 http.proxy = %PROXY_URL%
  goto :proxy_done
)

REM 扫描常见本地 HTTP 代理端口 (TCP 开着就认为存在)
echo [2/6] 未配置全局代理，自动扫描本机 HTTP 代理端口 …
set "CAND_PORTS=7890 7897 1080 10809 8080 3128"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ports=@(7890,7897,1080,10809,8080,3128); foreach($p in $ports){ try{$c=New-Object Net.Sockets.TcpClient;$c.Connect('127.0.0.1',$p);if($c.Connected){Write-Output $p;$c.Close();break;}}catch{}}" > "%TEMP%\_wx_proxy_port.tmp"
set /p FOUND_PORT=<"%TEMP%\_wx_proxy_port.tmp"
del "%TEMP%\_wx_proxy_port.tmp" 2>nul
if NOT "%FOUND_PORT%"=="" (
  set "PROXY_URL=http://127.0.0.1:%FOUND_PORT%"
  echo [2/6] 自动发现 127.0.0.1:%FOUND_PORT% 已监听 → 使用 %PROXY_URL%
  goto :proxy_done
)
echo [2/6] 未发现本地代理，将使用直连。

:proxy_done

REM ==== 3. 临时写入本仓库 git 配置（local 级别，不影响其他项目）====
echo.
echo [3/6] 写入本仓库临时 local git 配置（proxy / sslBackend / postBuffer）…
git config --local http.proxy       "" 2>nul
git config --local https.proxy      "" 2>nul
if NOT "%PROXY_URL%"=="" (
  git config --local http.proxy    "%PROXY_URL%"
  git config --local https.proxy   "%PROXY_URL%"
  echo       http.proxy = %PROXY_URL%
)
git config --local http.sslBackend  schannel
git config --local http.postBuffer  524288000
git config --local http.version     HTTP/1.1

REM ==== 4. 预验证 ls-remote（只读命令，快速检验 TLS + 凭据）====
echo.
echo [4/6] 预验证：git ls-remote origin main…（最长 25 秒）
set "LS_OK=0"
git ls-remote --exit-code --heads origin main >nul 2> "%TEMP%\_wx_ls.err"
if %ERRORLEVEL%==0 set "LS_OK=1"
if %LS_OK%==0 (
  echo       [FAIL] ls-remote 失败：
  type "%TEMP%\_wx_ls.err" 2>nul
  echo.
  echo       建议操作：
  echo       1) 确认浏览器能正常打开 https://github.com/SomnusWei/WxRead
  echo       2) 若 Clash/V2RayN 未启动，请先开启并选择可用节点
  echo       3) 强制指定代理： set WX_PUSH_PROXY=http://127.0.0.1:7890 ^& scripts\push_release.bat
  echo       4) 选择直连：   set WX_PUSH_PROXY=direct  ^& scripts\push_release.bat
  del "%TEMP%\_wx_ls.err" 2>nul
  goto :cleanup_exit_1
)
del "%TEMP%\_wx_ls.err" 2>nul
echo       [OK] GitHub 远程访问 + 凭据 正常。

REM ==== 5. push main ====
echo.
echo [5.1/6] 推送 main 分支 → origin/main …
git push origin main
if %ERRORLEVEL% NEQ 0 (
  echo       [FAIL] git push origin main 失败（错误码 %ERRORLEVEL%）。
  echo       如是因为之前本地 59b370e 已手动推送过 → 请改用：git push --force-with-lease origin main
  goto :cleanup_exit_1
)
echo       [OK] main 推送成功。

REM ==== 5.2 tag & push tag ====
echo.
echo [5.2/6] 处理 Tag %WX_PUSH_TAG% …
git rev-parse "%WX_PUSH_TAG}" >nul 2>nul
if %ERRORLEVEL%==0 (
  echo       本地已存在同名 Tag，跳过创建。
) else (
  git tag -a "%WX_PUSH_TAG%" -m "Release %WX_PUSH_TAG%: 3c1f206 (Paper Studio 整板 KPI + 发布管线 + 补丁分发基线)"
  if %ERRORLEVEL% NEQ 0 (
    echo       [WARN] 创建 Tag 失败，继续推送其他。
  )
)
git push origin "%WX_PUSH_TAG%" 2> "%TEMP%\_wx_tag.err"
if %ERRORLEVEL% NEQ 0 (
  echo       [WARN] 推送 Tag 失败（一般是同名 Tag 已存在远端）。信息：
  type "%TEMP%\_wx_tag.err" 2>nul
) else (
  echo       [OK] Tag %WX_PUSH_TAG% 推送到远端。
)
del "%TEMP%\_wx_tag.err" 2>nul

REM ==== 6. 验证 & 收尾 ====
:cleanup
echo.
echo [6/6] 清理临时 local 配置 & 验证最终状态 …
git config --local --unset http.proxy       2>nul
git config --local --unset https.proxy      2>nul
git config --local --unset http.sslBackend  2>nul
git config --local --unset http.postBuffer  2>nul
git config --local --unset http.version     2>nul

echo.
echo ====== 最终状态 ======
git status -sb
echo ======================
echo.
echo 🎉 推送完成！浏览器打开：
echo    https://github.com/SomnusWei/WxRead
echo    Releases / Tags 页面应出现：%WX_PUSH_TAG%

endlocal
exit /b 0

:cleanup_exit_1
REM 失败也要先清理配置
git config --local --unset http.proxy       2>nul
git config --local --unset https.proxy      2>nul
git config --local --unset http.sslBackend  2>nul
git config --local --unset http.postBuffer  2>nul
git config --local --unset http.version     2>nul
endlocal
exit /b 1
