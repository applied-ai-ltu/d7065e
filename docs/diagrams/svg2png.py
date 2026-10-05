"""Rasterize every SVG in this folder to a 2x PNG with a local Chromium-based browser
(D2's own PNG export needs a driver download that no longer resolves).

    BROWSER=/snap/brave/current/opt/brave.com/brave/brave python docs/diagrams/svg2png.py [name ...]
"""

import os
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
svgs = [HERE / f"{n}.svg" for n in sys.argv[1:]] or sorted(HERE.glob("*.svg"))
exe = os.environ.get("BROWSER")

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
    for svg in svgs:
        head = svg.read_text()[:2000]
        w = int(float(re.search(r'<svg[^>]*?\swidth="([\d.]+)"', head).group(1)))
        h = int(float(re.search(r'<svg[^>]*?\sheight="([\d.]+)"', head).group(1)))
        page = browser.new_page(viewport={"width": w + 20, "height": h + 20}, device_scale_factor=2)
        page.goto(svg.resolve().as_uri())
        page.query_selector("svg").screenshot(path=str(svg.with_suffix(".png")))
        page.pdf(path=str(svg.with_suffix(".pdf")), width=f"{w + 20}px", height=f"{h + 20}px", print_background=True)
        page.close()
        print(f"{svg.name} -> {svg.stem}.png + .pdf  ({w}x{h} @2x)")
    browser.close()
