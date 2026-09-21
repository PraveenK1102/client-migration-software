"""Run the mock target HR system as a STANDALONE HTTP service (its own SQLite).

By default the single-node app runs this same service in-process (config.target_inprocess=True)
behind an httpx ASGI transport — no port needed. This script is for running it as a genuinely
separate process instead, to demonstrate the HTTP boundary over a real port:

    # 1) start the target on :8100 (its own data/target.db)
    python scripts/run_target.py --port 8100
    # 2) point the app at it and disable the in-process transport
    TARGET_INPROCESS=false TARGET_BASE_URL=http://127.0.0.1:8100 uvicorn app.main:app

The migration/reconciliation code only ever talks to this via TargetEmployeeGateway; it never
reads the target's tables directly. This is the swap seam for a real remote target system.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the mock target HR system.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--db", default=str(BACKEND / "data" / "target.db"),
                    help="SQLite path for the target's OWN store (separate from the app DB).")
    args = ap.parse_args()

    os.environ["TARGET_DB_PATH"] = args.db
    import sys
    sys.path.insert(0, str(BACKEND))
    import uvicorn
    from mock_target.service import create_app

    uvicorn.run(create_app(args.db), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
