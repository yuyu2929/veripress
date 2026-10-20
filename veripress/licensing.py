"""企业许可证（Ed25519 签名，格式 VP1.<payload_b64>.<sig_b64>）。

为什么不自造一套加密：
采购方要能独立验签，所以必须是标准非对称签名 + 公钥随包分发。
私钥只在签发方手里（scripts/issue_license.py），永不入库。

许可门禁只拦"企业运营能力"（SSO / 合规报告 / 多组织），
核心的发布、版本链、账本、赏金一律不看许可证——那是开源内核的承诺。
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import load_pem_public_key

PREFIX = "VP1"
TIER_COMMUNITY = "community"
TIER_ENTERPRISE = "enterprise"

_PUBKEY_PATH = Path(__file__).parent / "license_public.pem"
_ENV_VAR = "VERIPRESS_LICENSE"

ENTERPRISE_FEATURES = {
    "sso": "SAML / OIDC 单点登录",
    "compliance_report": "合规对账报告（带平台签名，可交给审计）",
    "multi_org": "多组织隔离（一个实例服务多个研究所）",
}


@dataclass
class LicenseInfo:
    tier: str
    org: str
    seats: int
    features: tuple[str, ...]
    issued_at: float
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def days_left(self) -> int:
        return max(0, int((self.expires_at - time.time()) / 86400))

    def has(self, feature: str) -> bool:
        return feature in self.features


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_license(payload: dict, private_key_raw: bytes) -> str:
    body = _b64e(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())
    sig = _b64e(Ed25519PrivateKey.from_private_bytes(private_key_raw).sign(body.encode()))
    return f"{PREFIX}.{body}.{sig}"


def _load_public_key():
    if not _PUBKEY_PATH.exists():
        return None
    try:
        return load_pem_public_key(_PUBKEY_PATH.read_bytes())
    except Exception:
        return None


_cache: dict = {}


def parse(license_str: str, public_key=None) -> LicenseInfo | None:
    key = (license_str or "", id(public_key))
    if key in _cache:
        return _cache[key]
    info = _parse_uncached(license_str, public_key)
    _cache[key] = info
    return info


def _parse_uncached(license_str: str, public_key=None) -> LicenseInfo | None:
    if not license_str or not license_str.startswith(PREFIX + "."):
        return None
    parts = license_str.split(".")
    if len(parts) != 3:
        return None
    _, body, sig = parts
    pub = public_key or _load_public_key()
    if pub is None:
        return None
    try:
        pub.verify(_b64d(sig), body.encode())
    except Exception:
        return None          # 伪造/篡改 → 静默降级社区版
    try:
        payload = json.loads(_b64d(body))
        return LicenseInfo(
            tier=payload.get("tier", TIER_ENTERPRISE),
            org=payload.get("org", ""),
            seats=int(payload.get("seats", 1)),
            features=tuple(payload.get("features", ())),
            issued_at=float(payload.get("issued_at", 0)),
            expires_at=float(payload.get("expires_at", 0)),
        )
    except Exception:
        return None


def clear_cache() -> None:
    _cache.clear()


def current() -> LicenseInfo | None:
    import os

    return parse(os.environ.get(_ENV_VAR, "").strip())


def has_feature(feature: str) -> bool:
    info = current()
    return bool(info and not info.expired and info.has(feature))


def status() -> dict:
    info = current()
    return {
        "tier": info.tier if info and not info.expired else TIER_COMMUNITY,
        "org": info.org if info else "",
        "seats": info.seats if info else 0,
        "days_left": info.days_left if info else 0,
        "license_valid": bool(info and not info.expired),
        "features": {
            name: {"description": desc, "enabled": has_feature(name)}
            for name, desc in ENTERPRISE_FEATURES.items()
        },
    }
