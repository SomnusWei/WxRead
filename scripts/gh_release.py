"""通用 GitHub Release 创建 + 上传脚本（v2.2.5+ 通用）。

使用：
    1) 把 GitHub Personal Access Token（repo scope）写入：
           %TEMP%\\gh_token.txt     （UTF-8 / UTF-8 BOM 均支持）
       本脚本读完后立即删除该临时文件，避免泄露。
    2) 运行：
           python scripts\\gh_release.py --version 2.2.6 [--previous 2.2.5] [--draft]

逻辑：
    - repo = SomnusWei/WxRead；tag = v{VERSION}
    - 标题 = WxReadAssistant v{VERSION}
    - Body 来自 release/release_note-v{VERSION}.md
    - 上传：full.zip + setup.exe + 两个 sha1.txt（若存在 patch 也上传）
    - 自动探测本机代理（端口 7890 / 7897 / 1080），未发现则直连
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import ssl
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = "SomnusWei/WxRead"
PROXY_PORTS = [7890, 7897, 1080, 10809, 8080, 3128]


def detect_proxy() -> tuple[str, int] | None:
    """返回 (host, port) 或 None（直连）。"""
    s = urllib.request.getproxies().get("https") or urllib.request.getproxies().get("http")
    if s:
        try:
            from urllib.parse import urlparse
            p = urlparse(s)
            host = p.hostname or "127.0.0.1"
            port = p.port or 80
            return host, port
        except Exception:
            pass
    import socket
    for port in PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                return "127.0.0.1", port
        except OSError:
            continue
    return None


def load_token() -> str:
    tmp = Path(tempfile.gettempdir()) / "gh_token.txt"
    if not tmp.exists():
        raise SystemExit(
            f"[FAIL] 未找到 {tmp}\n"
            "请把 GitHub PAT（repo 权限）写入该文件后重试——本脚本读完会自动删除。"
        )
    token = tmp.read_text(encoding="utf-8-sig").strip()
    try:
        tmp.unlink()
    except OSError:
        pass
    if len(token) < 8:
        raise SystemExit("[FAIL] Token 看起来太短了。")
    return token


def proxy_connect(proxy: tuple[str, int] | None, host: str):
    ctx = ssl.create_default_context()
    if proxy:
        ph, pp = proxy
        conn = http.client.HTTPSConnection(ph, pp, context=ctx, timeout=120)
        conn.set_tunnel(host, 443)
    else:
        conn = http.client.HTTPSConnection(host, context=ctx, timeout=120)
    return conn


def api_call(proxy, host: str, method: str, path: str, headers: dict, body=None):
    conn = proxy_connect(proxy, host)
    import json
    if body is not None and isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        headers = dict(headers)
        headers["Content-Type"] = "application/json"
    for attempt in range(5):
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, data, dict(resp.getheaders())
        except (TimeoutError, OSError, ConnectionError) as exc:
            if attempt == 4:
                raise
            print(f"      [{attempt+1}/5] {exc.__class__.__name__}，1.5s 后重试…")
            time.sleep(1.5 * (attempt + 1))
            conn.close()
            conn = proxy_connect(proxy, host)
    # unreachable
    return 0, b"", {}


def sha1(p: Path) -> str:
    h = hashlib.sha1()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def upload(proxy, release_id: int, tag: str, asset_path: Path, mime: str) -> tuple[bool, str]:
    name = asset_path.name
    size = asset_path.stat().st_size
    print(f"   ↑ {name} ({size/1048576:.0f} MB, type={mime})")
    auth2 = {"Authorization": f"token {TOKEN}", "User-Agent": "WxRead-build"}
    auth2["Content-Type"] = mime
    auth2["Content-Length"] = str(size)
    t0 = time.time()
    with asset_path.open("rb") as fb:
        data = fb.read()
    # GitHub API: 16 位对齐上限 2GB，这里足够（512MB 级）
    status, resp, _ = api_call(
        proxy,
        "uploads.github.com",
        "POST",
        f"/repos/{REPO}/releases/{release_id}/assets?name=" + urllib.request.quote(name),
        auth2,
        data,
    )
    ok = status in (200, 201)
    msg = f"HTTP {status}  {int(time.time()-t0)}s"
    if not ok:
        msg += "  " + resp.decode(errors="replace")[:300]
    return ok, msg


# -----------------------------------------------------------------
# main
# -----------------------------------------------------------------
ap = argparse.ArgumentParser()
ap.add_argument("--version", required=True, help="例如 2.2.6")
ap.add_argument("--previous", default=None, help="例如 2.2.5（仅影响 patch 上传检测）")
ap.add_argument("--draft", action="store_true", help="先存为草稿再手动发布")
args = ap.parse_args()

VER = args.version.strip()
TAG = f"v{VER}"
TITLE = f"WxReadAssistant v{VER}"
ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT / "release"

TOKEN = load_token()
PROXY = detect_proxy()
if PROXY:
    print(f"[0/6] 代理 = {PROXY[0]}:{PROXY[1]}")
else:
    print("[0/6] 直连 GitHub（未发现本地 HTTP 代理）")

AUTH = {"Authorization": f"token {TOKEN}", "User-Agent": "WxRead-build",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"}

# ---- 1. release body: release_note + 下载摘要 ----
note_path = RELEASE_DIR / f"release_note-v{VER}.md"
if not note_path.exists():
    print(f"[WARN] 未找到 {note_path}，将用默认占位 body")
    body = f"## {TITLE}\n\n本版本二进制请查看 Assets。\n\n获取注册码请联系开发者：QQ 37784552 / Email somnusweiwei1989@outlook.com"
else:
    body = note_path.read_text(encoding="utf-8")

assets_to_upload: list[tuple[str, str]] = []  # (path, mime)
for fn, mime in [
    (f"WxReadAssistant-v{VER}-setup.exe", "application/vnd.microsoft.portable-executable"),
    (f"WxReadAssistant-v{VER}-setup.sha1.txt", "text/plain"),
    (f"WxReadAssistant-v{VER}-full.zip", "application/zip"),
    (f"WxReadAssistant-v{VER}-full.sha1.txt", "text/plain"),
]:
    p = RELEASE_DIR / fn
    if p.exists():
        assets_to_upload.append((str(p), mime))
    else:
        print(f"[WARN] 缺失 {fn}，跳过上传")
# patch（可选）
if args.previous:
    patch = RELEASE_DIR / f"patch-v{VER}-from-v{args.previous}.zip"
    if patch.exists():
        assets_to_upload.append((str(patch), "application/zip"))

print(f"[1/6] Release: tag={TAG} title={TITLE} draft={args.draft}")
print(f"[2/6] 待上传资源: {len(assets_to_upload)} 个")

# ---- 2. 查/创建 Release ----
print("[3/6] 创建 Release …")
payload = {
    "tag_name": TAG,
    "target_commitish": "main",
    "name": TITLE,
    "body": body,
    "draft": bool(args.draft),
    "prerelease": False,
    "make_latest": "true",
}
status, data, _ = api_call(PROXY, "api.github.com", "POST", f"/repos/{REPO}/releases", AUTH, payload)
if status == 422:  # 同名 tag/release 已存在
    print(f"      Release/tag 已存在（HTTP 422），尝试 GET /releases/tags/{TAG} 复用…")
    status, data, _ = api_call(PROXY, "api.github.com", "GET",
                               f"/repos/{REPO}/releases/tags/{TAG}", AUTH)
if status not in (200, 201):
    print(f"[FAIL] 创建 Release 失败 HTTP {status}：{data.decode(errors='replace')[:600]}")
    sys.exit(1)
import json as _json
release = _json.loads(data)
rid = release["id"]
html_url = release.get("html_url", "")
print(f"      Release ID = {rid}   →  {html_url or '(draft)'}")

# ---- 3. 上传 Assets（失败可重试：先删同名资源再重传） ----
ok_all = True
for idx, (path, mime) in enumerate(assets_to_upload, 1):
    p = Path(path)
    print(f"   [{idx+3}/{len(assets_to_upload)+3}] {p.name}")
    # 若已有同名 asset，先删（幂等重试）
    try:
        s1, list_r, _ = api_call(PROXY, "api.github.com", "GET",
                                 f"/repos/{REPO}/releases/{rid}/assets", AUTH)
        if s1 in (200, 201):
            for existing in _json.loads(list_r):
                if existing["name"] == p.name:
                    sid = existing["id"]
                    ds, _, _ = api_call(PROXY, "api.github.com", "DELETE",
                                        f"/repos/{REPO}/releases/assets/{sid}", AUTH)
                    print(f"      删除旧 asset {sid} → HTTP {ds}")
    except Exception:  # noqa: BLE001
        pass
    ok, msg = upload(PROXY, rid, TAG, p, mime)
    if ok:
        print(f"      ✓ {msg}")
    else:
        ok_all = False
        print(f"      ✗ {msg}")

# ---- 4. 收尾打印 ----
print()
if ok_all:
    print("🎉 GitHub Release 上传完成：")
else:
    print("⚠️  Release 已创建，但部分资源上传失败（见上方），请手动检查：")
print(f"   {html_url}")
print()
print(f"本地校验（sha1）：")
for idx, (path, _mime) in enumerate(assets_to_upload, 1):
    p = Path(path)
    print(f"   {sha1(p)}  {p.name}")
