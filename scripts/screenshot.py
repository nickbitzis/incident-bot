"""Capture README screenshots with headless Chrome. Start the server first: make run.

Usage: uv run python scripts/screenshot.py --port 8850
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
]
DOCS = Path(__file__).resolve().parents[1] / "docs"


def chrome() -> str:
    for c in CHROME_PATHS:
        if Path(c).exists() or shutil.which(c):
            return c
    raise SystemExit("Chrome not found")


def shoot(url: str, out: Path, width: int, height: int) -> None:
    args = [chrome(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
            f"--window-size={width},{height}", "--virtual-time-budget=6000",
            f"--screenshot={out}", url]
    subprocess.run(args, check=True, capture_output=True, timeout=90)
    print(f"wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8850)
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"
    with urllib.request.urlopen(f"{base}/api/incidents?state=identified") as r:
        live = json.load(r)["incidents"][0]
    with urllib.request.urlopen(f"{base}/api/incidents?severity=sev1") as r:
        sev1 = json.load(r)["incidents"][-1]["id"]
    DOCS.mkdir(exist_ok=True)
    shoot(f"{base}/#/chat/{live['channel_id']}", DOCS / "screenshot.png", 1400, 1180)
    shoot(f"{base}/#/metrics", DOCS / "metrics.png", 1400, 1250)
    shoot(f"{base}/#/incident/{sev1}", DOCS / "incident.png", 1400, 1800)
    shoot(f"{base}/#/status", DOCS / "status.png", 1000, 900)


if __name__ == "__main__":
    main()
