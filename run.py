"""Launch VibeReading using the local interpreter; see setup.ps1 for first setup."""

import argparse
import sys
import json
import urllib.request
from pathlib import Path


def main():
    if sys.version_info < (3, 12):
        raise SystemExit("VibeReading requires Python 3.12+.")
    parser = argparse.ArgumentParser(description="Independent local code-reading GUI")
    parser.add_argument("--project", type=Path, default=Path(__file__).parent / "examples" / "polyglot")
    parser.add_argument("--port", type=int, default=8871)
    parser.add_argument("--open", action="store_true", help="Open the local GUI in your browser")
    args = parser.parse_args()
    # Reopening start.cmd should reuse this app's running service, not fail on its port.
    if args.open:
        address = f"http://127.0.0.1:{args.port}"
        try:
            with urllib.request.urlopen(address + "/api/status", timeout=1) as response:
                existing = json.load(response)
            if existing.get("application") == "VibeReading":
                import webbrowser
                webbrowser.open(address)
                print(f"VibeReading is already running: {address}")
                return
        except (OSError, ValueError):
            pass
    try:
        from vibereading.server import serve
        serve(args.project, args.port, args.open)
    except ImportError as error:
        parser.exit(1, f"Missing local dependency: {error}\nRun setup.ps1 first.\n")
    except (ValueError, OSError) as error:
        parser.exit(1, f"Could not start: {error}\nUse --port to choose another local port.\n")


if __name__ == "__main__":
    main()
