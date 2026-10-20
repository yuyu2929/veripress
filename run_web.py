"""启动 Web 服务：python run_web.py [--db data/veripress.db] [--port 8600]

保留这个薄壳是为了兼容 README 与既有使用习惯；实际逻辑在 veripress/server.py。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from veripress.server import main  # noqa: E402

if __name__ == "__main__":
    main()
