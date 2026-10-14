"""Run the V2 parent control plane with Waitress."""

from __future__ import annotations

import argparse
import os

from waitress import serve

from model_migration.parent_app import create_parent_app, start_startup_model_reconciliation


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Azure-only V2 parent application.")
    parser.add_argument("--host", default=os.getenv("MODEL_PARENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MODEL_PARENT_PORT", "8765")))
    args = parser.parse_args()
    app = create_parent_app()
    start_startup_model_reconciliation(app)
    print(f"Parent model control plane listening on http://{args.host}:{args.port}")
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
