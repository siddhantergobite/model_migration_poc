from __future__ import annotations

import argparse
import sys
from pathlib import Path
from waitress import serve

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.web_factory import create_tutor_app

app, tutor = create_tutor_app(Path(__file__).resolve().parent)


def main() -> None:
    parser = argparse.ArgumentParser(description=f"Run {tutor['name']}'s student tutor")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=tutor["port"])
    args = parser.parse_args()
    print(f"{tutor['name']} is running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop this tutor server.")
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
