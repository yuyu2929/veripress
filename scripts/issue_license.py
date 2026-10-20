"""签发企业许可证。

用法：
    python scripts/issue_license.py --org "某大学 AI 研究院" --seats 25 \
        --features sso,compliance_report --days 365

输出一行 VP1.xxx.yyy，交给客户放进环境变量：
    export VERIPRESS_LICENSE=VP1....
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from veripress.licensing import ENTERPRISE_FEATURES, sign_license  # noqa: E402

KEY_FILE = ROOT / "keys" / "license.key"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--seats", type=int, default=5)
    ap.add_argument("--features", default="sso,compliance_report",
                    help=f"逗号分隔，可选：{','.join(ENTERPRISE_FEATURES)}")
    ap.add_argument("--days", type=int, default=365)
    args = ap.parse_args()

    if not KEY_FILE.exists():
        print("找不到 keys/license.key，先运行 python scripts/gen_keys.py")
        return 1

    feats = [f.strip() for f in args.features.split(",") if f.strip()]
    unknown = [f for f in feats if f not in ENTERPRISE_FEATURES]
    if unknown:
        print(f"未知功能：{unknown}；可选 {list(ENTERPRISE_FEATURES)}")
        return 1

    now = time.time()
    payload = {
        "tier": "enterprise",
        "org": args.org,
        "seats": args.seats,
        "features": feats,
        "issued_at": now,
        "expires_at": now + args.days * 86400,
    }
    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(KEY_FILE.read_text().strip()))
    print(sign_license(payload, priv.private_bytes_raw()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
