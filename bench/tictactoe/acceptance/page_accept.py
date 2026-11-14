"""Hidden page check: serve the repo, drive it in headless Edge, screenshot it.

    python acceptance/page_accept.py <repo> <screenshot.png>

Prints JSON. The probe page is copied into the repo only while the check runs
and is removed afterwards, so nothing of the acceptance suite is left behind.
"""

from __future__ import annotations

import functools
import http.server
import json
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
HERE = Path(__file__).resolve().parent
PROBE = "__acceptance_probe.html"
# Fixed, so a probe that aborts early scores against the whole check, not
# against the part it reached.
TOTAL = 14


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401 - silence
        pass


def check(root: Path, screenshot: Path) -> dict:
    shutil.copy(HERE / "probe.html", root / PROBE)
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/{PROBE}"
    try:
        dom = subprocess.run(
            [EDGE, "--headless=new", "--disable-gpu", "--window-size=720,1000",
             "--virtual-time-budget=15000", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        ).stdout
        subprocess.run(
            [EDGE, "--headless=new", "--disable-gpu", "--window-size=700,900",
             "--virtual-time-budget=15000", f"--screenshot={screenshot}", url],
            capture_output=True, timeout=120,
        )
    finally:
        server.shutdown()
        (root / PROBE).unlink(missing_ok=True)
    match = re.search(r'<pre id="result">(.*?)</pre>', dom, re.DOTALL)
    raw = match.group(1) if match else "pending"
    try:
        checks = json.loads(raw.replace("&quot;", '"').replace("&amp;", "&")
                            .replace("&lt;", "<").replace("&gt;", ">"))
    except ValueError:
        return {"passed": 0, "total": TOTAL, "checks": {"probe ran": raw[:300]}}
    passed = sum(1 for v in checks.values() if v is True)
    return {"passed": passed, "total": TOTAL, "checks": checks}


if __name__ == "__main__":
    print(json.dumps(check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()), indent=1))
