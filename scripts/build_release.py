r"""WxReadAssistant 一键打包 & 补丁分发脚本。

使用方式（必须在项目根目录 e:\item\wxread 执行）：

    # 1) 构建 2.2.0：首次（或已知上一版 dist 不在磁盘）产出 full 包
    python scripts\build_release.py --version 2.2.0

    # 2) 构建 2.2.1 并基于上一版（2.2.0）产出 增量补丁包 + full 包
    python scripts\build_release.py --version 2.2.1 --previous 2.2.0

    # 3) 先跑语法检查 / 冒烟（可跳过）
    python scripts\build_release.py --version 2.2.1 --skip-smoke

脚本职责：
  1. 语法校验（py_compile）所有 app 模块
  2. 将版本号 + build_id（git short sha + 日期）写入 app/ui/icon_store.py
     （常量覆盖：APP_VERSION / APP_BUILD_ID）
  3. 执行 PyInstaller onedir（WxReadAssistant.spec）
  4. 将 dist/WxReadAssistant 复制到 release/WxReadAssistant-v{VERSION}-full
  5. 压缩为 -full.zip（用户整包下载）
  6. 若传 --previous：和 release/WxReadAssistant-v{PREVIOUS}-full 做 diff，
     只打包变更/新增文件 → release/patch-v{VERSION}-from-v{PREVIOUS}.zip
     并同步生成 apply_patch.bat（双击即可覆盖更新，用户数据目录不动）
  7. 调用 Inno Setup（ISCC.exe）构建 Windows 安装包
     → release/WxReadAssistant-v{VERSION}-setup.exe（含全部依赖，双击安装）
     未安装 Inno Setup 时自动跳过（winget install JRSoftware.InnoSetup）
  8. 生成 release_note-v{VERSION}.md
"""
from __future__ import annotations

import argparse
import ast
import datetime as _dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
APP_UI = ROOT / "app" / "ui"
ICON_STORE = APP_UI / "icon_store.py"
SPEC_FILE = ROOT / "WxReadAssistant.spec"
DIST_DIR = ROOT / "dist" / "WxReadAssistant"
RELEASE_DIR = ROOT / "release"


# =====================================================================
# 工具
# =====================================================================
def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    print(f"\n> {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if r.stdout:
        print(r.stdout)
    if r.stderr:
        print(r.stderr, file=sys.stderr)
    if check and r.returncode != 0:
        raise SystemExit(f"[FAIL] 命令返回 {r.returncode}: {' '.join(cmd)}")
    return r


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_short_sha() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip() or "nogit"
    except Exception:  # noqa: BLE001
        pass
    return "nogit"


# =====================================================================
# 1. 语法检查
# =====================================================================
def syntax_check() -> None:
    print("[1/8] 语法检查 …")
    target_files = [
        ROOT / "main.py",
        *list((ROOT / "app").rglob("*.py")),
    ]
    for p in target_files:
        subprocess.run(
            [sys.executable, "-m", "py_compile", str(p)],
            cwd=str(ROOT), check=True,
        )
    print("   ✓ 语法 OK（" + str(len(target_files)) + " 个 .py）")


# =====================================================================
# 2. 写入版本号
# =====================================================================
def _rewrite_icon_store(version: str, build_id: str) -> None:
    """改写 icon_store.py 里的 APP_VERSION / APP_BUILD_ID（按名按字面量精确替换）。"""
    print(f"[2/8] 写入版本号 APP_VERSION={version}  APP_BUILD_ID={build_id}")
    src = ICON_STORE.read_text(encoding="utf-8")

    def _repl_version(m: re.Match) -> str:
        return f'{m.group(1)}APP_VERSION = "{version}"{m.group(3)}'

    def _repl_build(m: re.Match) -> str:
        return f'{m.group(1)}APP_BUILD_ID = "{build_id}"{m.group(3)}'

    new_src, n1 = re.subn(
        r"(^[ \t]*)APP_VERSION[ \t]*=[ \t]*\"([^\"]*)\"([ \t]*.*$)",
        _repl_version, src, flags=re.M,
    )
    new_src, n2 = re.subn(
        r"(^[ \t]*)APP_BUILD_ID[ \t]*=[ \t]*\"([^\"]*)\"([ \t]*.*$)",
        _repl_build, new_src, flags=re.M,
    )
    if n1 < 1 or n2 < 1:
        raise SystemExit(
            "[FAIL] icon_store.py 中未找到 APP_VERSION / APP_BUILD_ID 字面量，"
            "请确认文件结构。"
        )
    # AST 语法自检（写入后必须仍可解析）
    try:
        ast.parse(new_src)
    except SyntaxError as exc:  # pragma: no cover - defensive
        raise SystemExit(f"[FAIL] 重写后 icon_store.py 语法错误: {exc}") from exc
    ICON_STORE.write_text(new_src, encoding="utf-8")


# =====================================================================
# 3. PyInstaller 构建 onedir
# =====================================================================
def pyinstaller_build() -> None:
    print("[3/8] PyInstaller onedir 构建 …（耗时约 3~6 分钟）")
    if not SPEC_FILE.exists():
        raise SystemExit(f"[FAIL] 未找到 spec 文件：{SPEC_FILE}")
    # 清理上次 dist/WxReadAssistant 确保干净
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR, ignore_errors=True)
    # PyInstaller
    run([
        sys.executable, "-m", "PyInstaller",
        str(SPEC_FILE), "--noconfirm", "--clean",
    ])
    if not DIST_DIR.exists():
        raise SystemExit(
            f"[FAIL] PyInstaller 完成但未找到产物 {DIST_DIR}"
        )
    exe = DIST_DIR / "WxReadAssistant.exe"
    if not exe.exists():
        raise SystemExit(f"[FAIL] onedir 产物中缺失 {exe.name}")
    # 打印大小统计（便于发现异常膨胀）
    size_mb = sum(f.stat().st_size for f in DIST_DIR.rglob("*") if f.is_file()) / 1024 / 1024
    print(f"   ✓ onedir 完成，总大小 ≈ {size_mb:.0f} MB")


# =====================================================================
# 4. 拷贝 → release/WxReadAssistant-vX.Y.Z-full
# =====================================================================
def stage_full(version: str) -> Path:
    print(f"[4/8] 发布区归档 full 目录（版本 {version}）…")
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    full_dir = RELEASE_DIR / f"WxReadAssistant-v{version}-full"
    if full_dir.exists():
        shutil.rmtree(full_dir, ignore_errors=True)
    shutil.copytree(DIST_DIR, full_dir)
    return full_dir


# =====================================================================
# 5. 打包 full.zip + 写 sha1 清单
# =====================================================================
def zip_full(version: str, full_dir: Path) -> Path:
    print(f"[5/8] 压缩 full 包 → release/WxReadAssistant-v{version}-full.zip …")
    zip_path = RELEASE_DIR / f"WxReadAssistant-v{version}-full.zip"
    manifest_lines = [
        f"# WxReadAssistant v{version}  完整安装包",
        f"# 打包时间: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "# 文件清单（相对 WxReadAssistant/ 目录；path = sha1）",
        "",
    ]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(full_dir.rglob("*")):
            if not p.is_file():
                continue
            arcname = Path("WxReadAssistant") / p.relative_to(full_dir)
            zf.write(p, arcname=str(arcname))
            try:
                manifest_lines.append(
                    f"{arcname.as_posix()} = {sha1_file(p)}"
                )
            except OSError:
                manifest_lines.append(f"{arcname.as_posix()} = <unreadable>")
    # 写 sha1 清单（便于用户比对补丁是否完整）
    manifest_path = RELEASE_DIR / f"WxReadAssistant-v{version}-full.sha1.txt"
    manifest_path.write_text("\n".join(manifest_lines), encoding="utf-8")
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"   ✓ {zip_path.name}  大小 ≈ {size_mb:.0f} MB")
    return zip_path


# =====================================================================
# 6. 增量补丁（仅当提供 --previous）
# =====================================================================
def make_patch(version: str, previous: str, full_dir: Path) -> Path | None:
    prev_full = RELEASE_DIR / f"WxReadAssistant-v{previous}-full"
    if not prev_full.exists():
        print(
            f"[6/8] ⚠️  未找到上一版目录 {prev_full}，跳过增量补丁。"
            f"请先把 v{previous} full 目录归档到 release/ 下再运行。"
        )
        return None
    print(f"[6/8] 计算差异并生成 patch-v{version}-from-v{previous}.zip …")
    prev = {p.relative_to(prev_full): p for p in prev_full.rglob("*") if p.is_file()}
    curr = {p.relative_to(full_dir): p for p in full_dir.rglob("*") if p.is_file()}

    changed_or_new = []
    for rel, p in curr.items():
        q = prev.get(rel)
        if q is None:
            changed_or_new.append((rel, "A"))
            continue
        # 快速比较（大小不同 → 变更；大小相同用 sha1）
        if p.stat().st_size != q.stat().st_size:
            changed_or_new.append((rel, "M"))
        elif sha1_file(p) != sha1_file(q):
            changed_or_new.append((rel, "M"))

    deleted = [rel for rel in prev.keys() if rel not in curr]

    zip_path = RELEASE_DIR / f"patch-v{version}-from-v{previous}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1) 写入所有 A/M 文件到 WxReadAssistant/ 前缀
        for rel, _flag in changed_or_new:
            src = full_dir / rel
            arcname = Path("WxReadAssistant") / rel
            zf.write(src, arcname=str(arcname))
        # 2) 写入 patch_meta.txt（供 apply_patch.bat 判断是否需要删除旧文件）
        meta = [
            f"patch_from=v{previous}",
            f"patch_to=v{version}",
            f"built_at={_dt.datetime.now().isoformat(timespec='seconds')}",
            "",
            "# delete_list（相对 WxReadAssistant/ 目录，补丁应用时先删同名再覆盖，"
            "再删 delete_list 中剩余项）",
            *[f"D {rel.as_posix()}" for rel in sorted(deleted)],
        ]
        zf.writestr("patch_meta.txt", "\n".join(meta))
        # 3) 写入 apply_patch.bat（用户双击：自动覆盖 + 删旧文件 + 备份旧目录）
        bat = _apply_patch_bat_script(version, previous)
        zf.writestr("apply_patch.bat", bat)
        # 4) 附带一个极简 README
        readme = (
            f"# WxReadAssistant 补丁 v{version}\n\n"
            f"本补丁用于从 v{previous} 升级到 v{version}。\n\n"
            "## 使用步骤\n\n"
            "1. 关闭 WxReadAssistant（含托盘）。\n"
            "2. 把这个 zip **解压**到你的 **WxReadAssistant 安装目录（父级）**，\n"
            "   保证解压后目录结构：\n"
            "       - 安装根目录/\n"
            "         ├── WxReadAssistant/         (现有 onedir 目录)\n"
            "         └── apply_patch.bat\n"
            "3. 右键“以管理员身份运行” apply_patch.bat（或双击，首次若被 SmartScreen 拦截\n"
            "   请点「更多信息 → 仍要运行」）。\n"
            "4. 启动桌面上的 WxReadAssistant，打开「⚙️ 配置中心」或主界面右下版本胶囊，\n"
            f"   确认显示 v{version}。\n\n"
            "## 补丁做了什么（详见 apply_patch.bat）\n\n"
            "- 备份旧的 WxReadAssistant 到 _backup_v{previous}_<时间戳>/ （失败可手动回滚）\n"
            "- 用 patch 内 WxReadAssistant/ 目录覆盖同名文件（代码资源）\n"
            "- 删除 delete_list 中的旧文件（已移除的模块）\n"
            "- 用户数据目录（%APPDATA%\\\\WxReadAssistant）完全不动\n"
        )
        zf.writestr("README-补丁使用说明.md", readme)

    print(
        f"   ✓ 补丁共 {len(changed_or_new)} 项变更/新增 + {len(deleted)} 项删除"
    )
    return zip_path


def _apply_patch_bat_script(version: str, previous: str) -> str:
    """生成一个用户可双击的升级批处理（防御性：先备份、再覆盖、再删除）。

    批处理约定的运行目录：解压后，与 WxReadAssistant/ 目录处于**同一父级**。
    """
    return r"""@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM ============================================================
REM   WxReadAssistant 增量补丁：从 v""" + previous + r""" → v""" + version + r"""
REM   用法：把补丁 zip 解压到「WxReadAssistant\」的**父目录**，双击本 bat
REM ============================================================
title WxReadAssistant 补丁 v""" + version + r""" (from v""" + previous + r""")

REM 定位：脚本所在目录 = 补丁解压根
set "PATCH_ROOT=%~dp0"
set "PATCH_ROOT=%PATCH_ROOT:~0,-1%"
set "TARGET=%PATCH_ROOT%\WxReadAssistant"

REM --- 1. 安全检查 ---
if not exist "%TARGET%\WxReadAssistant.exe" (
    echo.
    echo [错误] 未找到 %TARGET%\WxReadAssistant.exe
    echo 请把补丁 zip 解压到 WxReadAssistant\ 目录的**父目录**，
    echo 使本 bat 与 WxReadAssistant\ 在同一级目录。
    echo.
    pause
    exit /B 1
)

REM --- 2. 进程占位检查（未找到 exe 也允许继续，避免误杀） ---
tasklist /FI "IMAGENAME eq WxReadAssistant.exe" 2>nul | find /I "WxReadAssistant.exe" >nul
if %ERRORLEVEL% EQU 0 (
    echo.
    echo [警告] 检测到 WxReadAssistant.exe 正在运行！
    echo 请先关闭主程序 + 退出托盘（右键托盘图标 → 退出程序），再重新运行本补丁。
    pause
    exit /B 2
)

REM --- 3. 备份旧版到 _backup_vPREV_YYYYMMDD_HHMMSS ---
for /f "tokens=2 delims==" %%I in (
    'wmic os get LocalDateTime /VALUE 2^>nul'
) do if ".%%I." neq ".." set "ts=%%I"
set "STAMP=%ts:~0,8%_%ts:~8,6%"
set "BACKUP=%PATCH_ROOT%\_backup_v""" + previous + r"""_%STAMP%"
echo.
echo [1/4] 备份旧版到 %BACKUP% ...
robocopy "%TARGET%" "%BACKUP%" /E /R:3 /W:2 /NP >nul
if %ERRORLEVEL% GEQ 8 (
    echo [错误] 备份失败，请手动复制 WxReadAssistant\ 后重试。
    pause
    exit /B 3
)
echo       完成。

REM --- 4. 覆盖新文件（补丁内 WxReadAssistant\ → 目标 WxReadAssistant\） ---
set "SRC=%PATCH_ROOT%\WxReadAssistant"
if not exist "%SRC%" (
    echo [错误] 补丁 zip 解压不完整：缺少 WxReadAssistant\ 目录。
    pause
    exit /B 4
)
echo [2/4] 覆盖新文件到 %TARGET% ...
robocopy "%SRC%" "%TARGET%" /E /R:3 /W:2 /NP >nul
if %ERRORLEVEL% GEQ 8 (
    echo [错误] 覆盖失败。请临时关闭杀毒软件或右键“以管理员身份运行”重试。
    pause
    exit /B 5
)
echo       完成。

REM --- 5. 按 patch_meta.txt 删除已移除文件 ---
set "META=%PATCH_ROOT%\patch_meta.txt"
echo [3/4] 清理已移除的旧文件（清单来自 patch_meta.txt）...
if exist "%META%" (
    for /f "usebackq tokens=1*" %%L in ("%META%") do (
        if /I "%%L"=="D" (
            if exist "%TARGET%\%%M" (
                del /F /Q "%TARGET%\%%M" >nul 2>&1
            )
        )
    )
)
echo       完成。

REM --- 6. 成功提示 ---
echo.
echo [4/4] 补丁应用完成：v""" + previous + r""" -> v""" + version + r"""
echo.
echo       回滚方式：把 %BACKUP%\ 整个目录重命名为 WxReadAssistant\ 即可。
echo       用户数据保存在 %%APPDATA%%\WxReadAssistant\ ，本次补丁完全未触碰。
echo.
pause
exit /B 0
"""


# =====================================================================
# 7. Inno Setup 安装包（setup.exe，含全部依赖）
# =====================================================================
ISCC_CANDIDATES = [
    Path(os.environ.get("ISCC_EXE", "")) if os.environ.get("ISCC_EXE") else None,
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"
    if os.environ.get("LOCALAPPDATA") else None,
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
]


def find_iscc() -> Path | None:
    for p in ISCC_CANDIDATES:
        try:
            if p and p.is_file():
                return p
        except OSError:
            continue
    which = shutil.which("ISCC")
    return Path(which) if which else None


def build_installer(version: str, full_dir: Path) -> Path | None:
    iss_file = ROOT / "installer" / "wxread_assistant.iss"
    if not iss_file.exists():
        print(f"[7/8] ⚠️  未找到 {iss_file}，跳过安装包构建。")
        return None
    iscc = find_iscc()
    if iscc is None:
        print(
            "[7/8] ⚠️  未找到 Inno Setup（ISCC.exe），跳过安装包构建。\n"
            "       安装后重试：winget install --id JRSoftware.InnoSetup -e"
        )
        return None
    print(f"[7/8] Inno Setup 构建安装包（{iscc}）…")
    run([
        str(iscc),
        f"/DAppVersion={version}",
        f"/DDistDir={full_dir}",
        f"/DOutputDir={RELEASE_DIR}",
        str(iss_file),
    ])
    setup_exe = RELEASE_DIR / f"WxReadAssistant-v{version}-setup.exe"
    if not setup_exe.exists():
        raise SystemExit(f"[FAIL] ISCC 完成但未找到产物 {setup_exe}")
    # setup.exe 的 sha1 清单（便于发布时校验）
    sha1_path = RELEASE_DIR / f"WxReadAssistant-v{version}-setup.sha1.txt"
    sha1_path.write_text(
        f"{sha1_file(setup_exe)}  {setup_exe.name}\n", encoding="utf-8"
    )
    size_mb = setup_exe.stat().st_size / 1024 / 1024
    print(f"   ✓ {setup_exe.name}  大小 ≈ {size_mb:.0f} MB")
    return setup_exe


# =====================================================================
# 8. release_note 自动生成
# =====================================================================
def write_release_note(
    version: str, previous: str | None, full_zip: Path, setup_exe: Path | None
) -> Path:
    print(f"[8/8] 生成 release_note-v{version}.md …")
    build_date = _dt.datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 📦 WxReadAssistant v{version} — Release Note",
        f"> 打包日期：{build_date}  ·  构建 SHA：{git_short_sha()}",
        "",
        "## 下载地址",
        "",
        "| 文件 | 说明 |",
        "|------|------|",
        (
            f"| `WxReadAssistant-v{version}-setup.exe` | **Windows 安装包（推荐）**："
            "双击安装，含全部依赖，自动创建开始菜单/桌面快捷方式，自带卸载器 |"
        ) if setup_exe else (
            "| `WxReadAssistant-v{v}-setup.exe` | （本次未构建安装包：需安装 "
            "[Inno Setup](https://jrsoftware.org/isinfo.php) 后重新打包） |".format(v=version)
        ),
        f"| `WxReadAssistant-v{version}-full.zip` | 完整免安装包（解压即用） |",
        (
            f"| `patch-v{version}-from-v{previous}.zip` | **增量补丁**（"
            f"只适用于 v{previous} → v{version}，体积更小，见 README「补丁升级」） |"
        ) if previous and (RELEASE_DIR / f"patch-v{version}-from-v{previous}.zip").exists() else (
            "| — | —（本次未提供增量补丁，如需请指定 `--previous` 重新构建） |"
        ),
        "",
        "## 首次安装步骤",
        "",
        "### 方式一：安装包（推荐）",
        "",
        f"1. 双击 `WxReadAssistant-v{version}-setup.exe`，按向导完成安装（默认安装到 "
        "`C:\\Program Files\\WxReadAssistant\\`，非管理员可选仅为本机当前用户安装）。",
        "2. 从开始菜单 / 桌面快捷方式启动 `WxReadAssistant`。",
        "3. 首次使用：主页 → 「📷 扫码登录」→ 按提示扫码获取 Cookie。",
        "4. 配置：进入「⚙️ 配置中心」填入 **Skill API Key**（可选，用于官方阅读统计回写）。",
        "5. 卸载：Windows「设置 → 应用」或开始菜单卸载项；`%APPDATA%\\WxReadAssistant` "
        "用户数据（配置/进度/缓存）不会被删除。",
        "",
        "### 方式二：免安装 zip",
        "1. 解压 `WxReadAssistant-vX.Y.Z-full.zip` 到任意目录（建议 `C:\\Program Files\\WxReadAssistant\\`）。",
        "2. 进入子目录 `WxReadAssistant\\`，双击 `WxReadAssistant.exe` 启动。",
        "3. 首次使用：主页 → 「📷 扫码登录」→ 按提示扫码获取 Cookie。",
        "4. 配置：进入「⚙️ 配置中心」填入 **Skill API Key**（可选，用于官方阅读统计回写）。",
        "",
        "## 补丁升级（推荐后续版本都走这个）",
        "",
        "1. 关闭 WxReadAssistant（含托盘：右键托盘图标 → 退出）。",
        "2. 解压 `patch-vNEW-from-vOLD.zip` **到你的 WxReadAssistant 安装父目录**（"
        "与 `WxReadAssistant\\` 同层）。",
        "3. 双击 `apply_patch.bat`（或右键「以管理员身份运行」）。",
        "4. 脚本会自动备份旧版 → 覆盖新文件 → 清理旧文件。",
        "5. 重启 `WxReadAssistant.exe`，进入「⚙️ 配置中心」顶部或主界面右下版本胶囊，",
        f"   确认显示 `v{version}`。",
        "",
        "## v""" + version + r""" CHANGELOG（与 v""" + (previous or "?.?.?") + r""" 对比）""",
        "",
        "### 🎨 主界面（米白纸 Paper Studio）",
        "",
        "- 阅读统计 4 项取消「独立卡片」，改为**一整张整板**通栏 + 4 段间距 18px + 严格等宽；",
        "  4 段内容：标题 18px 墨黑 / 数值 36px 微信蓝 / aux 12px muted / 4px 进度条贴底留 8px。",
        "- 根因修复：之前卡内 VBox + MinimumExpanding/stretch(1) 会在 Windows Fusion 高 DPI 下",
        "  错误收缩 QLabel sizeHint → 标题/数值全行消失，本轮**全部改 Grid 硬预算**。",
        "- Header：左侧「📖 微信读书助手」Logo + 右日期胶囊 / Cookie 状态点 / Cookie 胶囊。",
        "- 版本号由「Cookie 旁」迁移到工作日志面板右下（小胶囊），窗口标题 / 托盘 ToolTip / 托盘",
        "  消息 / 配置中心标题旁也同步展示完整版本号（含 build_id）。",
        "",
        "### ⚙️ 配置中心",
        "",
        "- 2×3 Grid → 行 0：📖/🔑/📢；行 1：💻 系统（1 列） + 🧹 数据（**跨 col1+col2**）；",
        "  删除独立的「🎯 说明」组框，6 条建议**拆分到对应组框底部**作为 💡 inline tips。",
        "- 修复 SpinBox 上下箭头在高 DPI 渲染为「黑方块」Bug：使用 QProxyStyle 直接绘制",
        "  ▲/▼ 字符（JetBrains Mono 8pt 粗）。",
        "",
        "### 🚢 发布流程",
        "",
        "- 新增 `scripts/build_release.py`：一键 **版本号写入** → **PyInstaller onedir** →",
        "  **full.zip 归档** → **增量 patch 打包** → **release_note 生成**。",
        "- 所有 EXE 会把 **构建 SHA + 日期** 写入 `app/ui/icon_store.py::APP_BUILD_ID`，",
        "  主界面 / 日志 / 配置中心都可见，避免用户拿到同名旧二进制。",
        "",
        "### ✅ 校验清单",
        "",
        "- [ ] 首次安装：exe 启动无报错；扫码登录后状态点变绿。",
        "- [ ] 补丁升级：应用后窗口标题 / 版本胶囊显示新版次。",
        "- [ ] 配置中心 → 6 个 GroupBox 底部均有 1 条 inline 说明；SpinBox 箭头非黑方块。",
        "- [ ] 点击「停止 / 开始」按钮，日志追加 OK，阅读统计 4 段行高无裁字。",
        "",
        f"> 完整 sha1 清单见 `{full_zip.with_suffix('').name.replace('-full','')}`-full.sha1.txt",
        "",
    ]
    note_path = RELEASE_DIR / f"release_note-v{version}.md"
    note_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"   ✓ {note_path.name}")
    return note_path


# =====================================================================
# main
# =====================================================================
def main() -> None:
    ap = argparse.ArgumentParser(
        description="WxReadAssistant 发布打包：full 包 + 增量补丁 + release_note",
    )
    ap.add_argument("--version", required=True, help="版次，如 2.2.0")
    ap.add_argument("--previous", default=None, help="上一版，用于生成增量补丁，如 2.1.0")
    ap.add_argument("--skip-smoke", action="store_true", help="跳过启动烟雾（GUI 机器不阻塞）")
    args = ap.parse_args()

    if not SPEC_FILE.exists():
        raise SystemExit(f"[FAIL] 未在项目根找到 spec: {SPEC_FILE}，请先 cd 到 e:\\item\\wxread")

    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    build_stamp = _dt.datetime.now().strftime("%Y%m%d")
    build_id = f"{git_short_sha()} · {build_stamp}"

    # 步骤 1~8
    syntax_check()                                     # 1
    _rewrite_icon_store(args.version, build_id)        # 2

    # 2.5 可选 smoke（MainPage + SettingsPage 构造）
    if not args.skip_smoke:
        print("[2.5/8] 启动烟雾（MainWindow 构造 + apply_theme）…")
        subprocess.run([
            sys.executable, "-c",
            "import sys; sys.path.insert(0, '.'); "
            "from PySide6.QtWidgets import QApplication; "
            "from app.ui.main_window import MainWindow; "
            "app = QApplication.instance() or QApplication([]); "
            "w = MainWindow(); "
            "w.apply_theme('light'); "
            "print('SMOKE_OK_v" + args.version + "');",
        ], cwd=str(ROOT), check=True)
        print("   ✓ Smoke 通过")

    pyinstaller_build()                             # 3
    full_dir = stage_full(args.version)             # 4
    full_zip = zip_full(args.version, full_dir)     # 5
    make_patch(args.version, args.previous, full_dir) if args.previous else (
        print("[6/8] (未提供 --previous，跳过增量补丁)")
    )                                               # 6
    setup_exe = build_installer(args.version, full_dir)  # 7
    write_release_note(args.version, args.previous, full_zip, setup_exe)  # 8

    print("\n🎉 构建完成 → 产物位于 release/")
    for p in sorted(RELEASE_DIR.glob(f"*{args.version}*")):
        try:
            size_mb = p.stat().st_size / 1024 / 1024 if p.is_file() else sum(
                f.stat().st_size for f in p.rglob("*") if f.is_file()
            ) / 1024 / 1024
        except OSError:
            size_mb = -1
        print(f"    · {p.name}   (约 {size_mb:.0f} MB)")


if __name__ == "__main__":
    main()
