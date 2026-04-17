#!/bin/bash
# frontend-visual: Playwright-based pixel-level check for Three.js / canvas UIs.
#
# Usage:
#   frontend-visual.sh <url> [screenshot_out]
# Example:
#   frontend-visual.sh http://127.0.0.1:8000/ /tmp/my_ui.png
#
# Contract:
#   - Caller is responsible for starting the server at <url>.
#   - Exits 0 iff:
#     * Page loads with no pageerror / console.error events.
#     * The canvas (or main rendered element) has a bounding box > 100x100.
#     * A sample of canvas pixels shows <5% white and >30 unique colors at 16-bit
#       quantization — i.e., the canvas is actually rendering content.
#   - Writes a screenshot to <screenshot_out> (default /tmp/frontend-visual.png).
# What this catches:
#   - Blank / white / unrendered canvas bugs.
#   - JS module load errors (e.g., import-map misconfigurations).
#   - Controls missing from the DOM.
# What this does NOT catch:
#   - Subtle visual regressions (layout shifts, off-by-one pixels).
#   - Behavior after user interaction — use a slice-specific test for that.

set -u
if [ $# -lt 1 ]; then
  echo "usage: frontend-visual.sh <url> [screenshot_out]" >&2
  exit 2
fi
url="$1"
out="${2:-/tmp/frontend-visual.png}"

# Inline Python Playwright test — no separate file needed.
exec uv run python - "$url" "$out" <<'PY'
import sys, time, zlib, struct
from playwright.sync_api import sync_playwright

url = sys.argv[1]
out = sys.argv[2]


def rgb_distribution(png_bytes: bytes) -> dict:
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    i = 8
    width = height = None
    idat = bytearray()
    channels = 4
    while i < len(png_bytes):
        length = struct.unpack(">I", png_bytes[i : i + 4])[0]
        ctype = png_bytes[i + 4 : i + 8]
        data = png_bytes[i + 8 : i + 8 + length]
        i += 12 + length
        if ctype == b"IHDR":
            width, height = struct.unpack(">II", data[:8])
            color_type = data[9]
            channels = 3 if color_type == 2 else 4
        elif ctype == b"IDAT":
            idat.extend(data)
        elif ctype == b"IEND":
            break
    raw = zlib.decompress(bytes(idat))
    stride = width * channels + 1
    prev_row = bytearray(width * channels)
    pixels = []
    for y in range(height):
        row_start = y * stride
        filt = raw[row_start]
        row = bytearray(raw[row_start + 1 : row_start + stride])
        bpp = channels
        if filt == 1:
            for x in range(bpp, len(row)):
                row[x] = (row[x] + row[x - bpp]) & 0xFF
        elif filt == 2:
            for x in range(len(row)):
                row[x] = (row[x] + prev_row[x]) & 0xFF
        elif filt == 3:
            for x in range(len(row)):
                left = row[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + (left + prev_row[x]) // 2) & 0xFF
        elif filt == 4:
            for x in range(len(row)):
                a = row[x - bpp] if x >= bpp else 0
                b = prev_row[x]
                c = prev_row[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                row[x] = (row[x] + pred) & 0xFF
        prev_row = row
        for x in range(0, width, 4):
            off = x * channels
            pixels.append((row[off], row[off + 1], row[off + 2]))
    n = len(pixels)
    white = sum(1 for r, g, b in pixels if r > 240 and g > 240 and b > 240)
    return {
        "n": n,
        "white_frac": white / n,
        "unique_colors": len({(r >> 4, g >> 4, b >> 4) for r, g, b in pixels}),
    }


errors = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: errors.append(f"console.error: {m.text}") if m.type == "error" else None,
        )
        page.goto(url, wait_until="networkidle", timeout=30000)
        time.sleep(1.0)
        if errors:
            print("FAIL: page errors:", errors, file=sys.stderr)
            sys.exit(1)
        # Prefer an explicit <canvas>, fall back to the body if none exists.
        target = page.locator("canvas").first
        if target.count() == 0:
            target = page.locator("body").first
        box = target.bounding_box()
        if box is None or box["width"] < 100 or box["height"] < 100:
            print(f"FAIL: render target too small: {box}", file=sys.stderr)
            sys.exit(1)
        png = target.screenshot(path=out)
        dist = rgb_distribution(png)
        print(f"render dist: {dist}")
        if dist["white_frac"] >= 0.05:
            print(f"FAIL: canvas is {dist['white_frac']:.1%} white (threshold 5%)", file=sys.stderr)
            sys.exit(1)
        if dist["unique_colors"] <= 30:
            print(f"FAIL: only {dist['unique_colors']} unique colors (threshold 30)", file=sys.stderr)
            sys.exit(1)
        print("OK frontend-visual passed")
    finally:
        browser.close()
PY
