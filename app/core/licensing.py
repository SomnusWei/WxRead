"""本地授权与试用期管理（纯离线，无任何网络请求）。

注册码方案（16 位 XXXX-XXXX-XXXX-XXXX）
    10 字节载荷 → Crockford Base32 → 恰好 16 字符：
        b0     : 版本/类型（0x01 = 永久授权）
        b1..b4 : 随机 4 字节
        b5..b9 : HMAC-SHA256(SECRET, b0..b4) 前 5 字节（40-bit 截断）
    校验：本地重算 HMAC 比对。40-bit 截断 + 32B 密钥，手工枚举不可行。

试用期
    首次启动后 12 小时。时间戳双备份（local_db.json + 注册表），取最早值，
    防止单独删除其中一个文件重置试用期。过期后禁止开始阅读，
    可在「配置中心 → 授权管理」输入注册码激活。

密钥存储
    SECRET 以 XOR 混淆后内嵌（防 strings 直接提取；提高门槛而非绝对安全）。
    tools/keygen_app.py 与本模块共享同一算法与密钥。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets as _secrets
import time
from datetime import datetime
from typing import Any

from app.core.local_db import LocalDB

# ---------------------------------------------------------------------
# 常量与密钥
# ---------------------------------------------------------------------
TRIAL_HOURS = 12.0
CODE_VERSION = 0x01
REG_KEY = r"Software\WxReadAssistant"
REG_VALUE = "trial_first_run"

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford：无 I/L/O/U
_DECODE_MAP: dict[str, int] = {c: i for i, c in enumerate(_ALPHABET)}
_DECODE_MAP.update({"O": 0, "I": 1, "L": 1})  # 易混字符归一

_XOR_KEY = hashlib.sha256(b"WxReadAssistant::licensing::v1").digest()
_SECRET_B64 = "PKRIdFy57qXP+WmTdSF5BQIs5g9lWkg3ldCJzctRH70="


def _load_secret() -> bytes:
    obf = base64.b64decode(_SECRET_B64)
    return bytes(a ^ b for a, b in zip(obf, _XOR_KEY * ((len(obf) // len(_XOR_KEY)) + 1)))


_SECRET = _load_secret()

# ---------------------------------------------------------------------
# 注册码：生成 / 校验
# ---------------------------------------------------------------------
def generate_code(version: int = CODE_VERSION) -> str:
    """生成 16 位注册码（返回形如 XXXX-XXXX-XXXX-XXXX 的展示格式）。"""
    rnd = _secrets.token_bytes(4)
    mac = hmac.new(_SECRET, bytes([version]) + rnd, hashlib.sha256).digest()[:5]
    raw = bytes([version]) + rnd + mac  # 10 字节 = 80 bit
    n = int.from_bytes(raw, "big")
    chars = []
    for _ in range(16):  # 80 / 5 = 16，无填充
        chars.append(_ALPHABET[n & 0x1F])
        n >>= 5
    canonical = "".join(reversed(chars))
    return format_code(canonical)


def format_code(canonical16: str) -> str:
    """16 字符 → XXXX-XXXX-XXXX-XXXX。"""
    s = "-".join(canonical16[i : i + 4] for i in range(0, 16, 4))
    return s


_CONFUSABLE = {"O": "0", "I": "1", "L": "1"}  # 易混字符归一


def normalize_code(raw: str) -> str:
    """用户输入归一化：去分隔符、大写、易混字符映射。返回恰 16 字符或空串。"""
    s = re.sub(r"[^0-9A-Za-z]", "", str(raw)).upper()
    s = "".join(_CONFUSABLE.get(ch, ch) for ch in s)
    if len(s) != 16:
        return ""
    return s


def _b32_decode16(canonical: str) -> bytes | None:
    n = 0
    for ch in canonical:
        v = _DECODE_MAP.get(ch)
        if v is None or not isinstance(v, int):
            return None
        n = (n << 5) | v
    return n.to_bytes(10, "big")


def verify_code(raw: str) -> bool:
    """本地校验注册码（任何字符问题/长度不对/HMAC 不符都返回 False）。"""
    canonical = normalize_code(raw)
    if not canonical:
        return False
    payload = _b32_decode16(canonical)
    if payload is None:
        return False
    ver = payload[0]
    if ver != CODE_VERSION:
        return False
    mac = payload[5:10]
    expect = hmac.new(_SECRET, payload[:5], hashlib.sha256).digest()[:5]
    return hmac.compare_digest(mac, expect)


def code_hash(canonical16: str) -> str:
    """注册码本地指纹（存盘用，不存原文）。"""
    return hashlib.sha256(canonical16.encode("ascii") + _SECRET).hexdigest()


# ---------------------------------------------------------------------
# 试用期 / 激活状态
# ---------------------------------------------------------------------
def _registry_read_ts() -> float | None:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY) as k:
            val, _ = winreg.QueryValueEx(k, REG_VALUE)
            ts = float(val)
            return ts if ts > 0 else None
    except Exception:  # noqa: BLE001 (注册表不可用/非 Windows 一律忽略)
        return None


def _registry_write_ts(ts: float) -> None:
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, REG_VALUE, 0, winreg.REG_SZ, repr(ts))
    except Exception:  # noqa: BLE001
        pass


def _ensure_first_run(db: LocalDB) -> float:
    """获取/落盘首次启动时间戳（db + 注册表双备份，取最早）。"""
    now = time.time()
    db_ts = db.get_license().get("first_run_ts")
    db_ts = float(db_ts) if isinstance(db_ts, (int, float)) and db_ts > 0 else None
    reg_ts = _registry_read_ts()
    existing = [t for t in (db_ts, reg_ts) if t is not None]
    if existing:
        ts = min(existing)
    else:
        ts = now
    # 双写补齐（幂等；注册表只在缺失时写入，避免测试/调试回拨污染真实时间线）
    if db_ts is None:
        db.set_license({"first_run_ts": ts})
    if reg_ts is None:
        _registry_write_ts(ts)
    return ts


def get_status(db: LocalDB | None = None) -> dict[str, Any]:
    """返回授权状态。

    keys: licensed / expired / trial / hours_left / minutes_left /
          first_run_iso / masked_code
    licensed 优先于试用期（激活后永不过期）。
    """
    d = db or LocalDB()
    lic = d.get_license()
    first_run = _ensure_first_run(d)
    elapsed_h = max(0.0, (time.time() - first_run) / 3600.0)
    hours_left = max(0.0, TRIAL_HOURS - elapsed_h)
    licensed = bool(lic.get("code_hash"))
    return {
        "licensed": licensed,
        "expired": (not licensed) and hours_left <= 0.0,
        "trial": not licensed,
        "hours_left": round(hours_left, 3),
        "minutes_left": int(hours_left * 60),
        "first_run_iso": datetime.fromtimestamp(first_run).strftime("%Y-%m-%d %H:%M"),
        "masked_code": str(lic.get("masked_code", "")),
    }


def activate(raw_code: str, db: LocalDB | None = None) -> tuple[bool, str]:
    """校验并持久化注册码。返回 (是否成功, 提示信息)。"""
    canonical = normalize_code(raw_code)
    if not canonical:
        return False, "注册码格式不正确（应为 16 位 XXXX-XXXX-XXXX-XXXX）"
    if not verify_code(canonical):
        return False, "注册码无效，请核对后重试"
    masked = format_code(canonical)
    d = db or LocalDB()
    d.set_license(
        {
            "code_hash": code_hash(canonical),
            "masked_code": masked,
            "activated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    return True, f"激活成功：{masked}"


def deactivate(db: LocalDB | None = None) -> None:
    """清除激活状态（仅调试/售后用）。"""
    own = db is None
    d = db or LocalDB()
    lic = d.get_license()
    lic.pop("code_hash", None)
    lic.pop("masked_code", None)
    lic.pop("activated_at", None)
    d.set_license(lic)
