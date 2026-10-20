"""生成两对 Ed25519 密钥：

1. 许可证密钥（签发企业许可）
   keys/license.key          ← 私钥，绝不入库
   veripress/license_public.pem ← 公钥，随包分发，用于验签许可证

2. 合规报告签名密钥（给审计方的可验证证明）
   keys/compliance.key       ← 私钥，绝不入库
   veripress/compliance_public.pem ← 公钥，交给审计方验签

用法：
    python scripts/gen_keys.py            # 只在不存在时生成
    python scripts/gen_keys.py --force    # 覆盖（旧许可证将全部失效）
"""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parent.parent
KEYS = ROOT / "keys"
PKG = ROOT / "veripress"


def _write_private(path: Path, raw: bytes, force: bool) -> bool:
    if path.exists() and not force:
        print(f"跳过（已存在）：{path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw.hex(), encoding="ascii")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    print(f"写入私钥：{path}（hex，已 gitignore）")
    return True


def _write_public(path: Path, pk, force: bool) -> bool:
    if path.exists() and not force:
        print(f"跳过（已存在）：{path}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = pk.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    path.write_bytes(pem)
    print(f"写入公钥：{path}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖已有密钥（会让旧许可证失效）")
    args = ap.parse_args()

    lic = Ed25519PrivateKey.generate()
    if _write_private(KEYS / "license.key", lic.private_bytes_raw(), args.force):
        _write_public(PKG / "license_public.pem", lic, True)

    comp = Ed25519PrivateKey.generate()
    if _write_private(KEYS / "compliance.key", comp.private_bytes_raw(), args.force):
        _write_public(PKG / "compliance_public.pem", comp, True)

    print("\n把公钥提交，私钥留在本机。签发许可证见 scripts/issue_license.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
