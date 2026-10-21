# -*- coding: utf-8 -*-
"""用 Playwright 给运行中的界面截图，供 README 使用。

用法：
    python agscanner.py --no-browser &
    python scripts/capture_screenshots.py
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

CHROME = r"D:\soft\playwright-chrome-win64\chrome-win64\chrome.exe"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
URL = "http://127.0.0.1:8765/"

OUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2)
    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(2500)

    page.screenshot(path=str(OUT / "01-overview.png"), full_page=True)

    page.click('button[data-f="pure"]')
    page.wait_for_timeout(600)
    page.screenshot(path=str(OUT / "02-purity.png"), full_page=True)

    page.click('button[data-f="all"]')
    page.wait_for_timeout(400)
    page.screenshot(path=str(OUT / "03-controls.png"),
                    clip={"x": 0, "y": 0, "width": 1600, "height": 330})

    table = page.locator("table").first
    box = table.bounding_box()
    page.screenshot(path=str(OUT / "04-table.png"),
                    clip={"x": box["x"], "y": box["y"], "width": box["width"],
                          "height": min(620, box["height"])})
    browser.close()

for f in sorted(OUT.glob("*.png")):
    print("%-24s %.0f KB" % (f.name, f.stat().st_size / 1024))
