"""服务器入口：`veripress --db data/veripress.db --port 8600`。

同时被 run_web.py 复用，避免两套参数解析。
"""
from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="VeriPress Web 服务")
    parser.add_argument("--db", default="data/veripress.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = parser.parse_args()

    from .api import create_app
    from .storage import Storage

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = Storage(args.db)
    app = create_app(store)

    import uvicorn

    print(f"VeriPress → http://{args.host}:{args.port}  (db: {args.db})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                reload=args.reload)


if __name__ == "__main__":
    main()
