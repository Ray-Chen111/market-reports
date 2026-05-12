import json
import os
import time
import urllib.request
from pathlib import Path

from market_report import send_line_card


ROOT = Path(__file__).resolve().parents[1]


def wait_until_ready(url: str, attempts: int = 24, delay: int = 10):
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "market-report-bot/1.0"})
            with urllib.request.urlopen(req, timeout=20) as response:
                if 200 <= response.status < 300:
                    return True
        except Exception as exc:
            print(f"Waiting for Pages ({i + 1}/{attempts}): {exc}")
        time.sleep(delay)
    return False


def main():
    last_run = json.loads((ROOT / "last-run.json").read_text(encoding="utf-8"))
    meta_path = ROOT / "site" / "reports" / f"{last_run['slug']}.meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        meta = {
            "title": "市場快報",
            "date_label": last_run.get("generated_at_utc", ""),
            "summary": "報告已產生，請點查看完整報告。",
            "temperature": "",
            "risks": "",
            "themes": "",
            "button_label": "查看完整報告",
        }
    url = last_run["url"]
    if not wait_until_ready(url):
        raise RuntimeError(f"Pages URL is still not ready: {url}")
    send_line_card(meta, url)
    print(url)


if __name__ == "__main__":
    main()
