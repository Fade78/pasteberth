#!/usr/bin/env python3
"""Render the self-contained website with Chromium and Playwright.

python tools/render.py --width 1600 --height 1000 --lang en --output desktop.png

No HTTP navigation: the HTML is loaded in memory, like the supplied renderer.
Requires playwright and a Chromium executable; --chromium overrides its path.
"""
from __future__ import annotations
import argparse
import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]

async def render(args: argparse.Namespace) -> None:
    if not args.output.resolve().is_relative_to(ROOT):
        raise ValueError('Render output must stay inside site/')
    html = args.input.read_text(encoding='utf-8')
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=args.chromium,
            headless=True, args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'])
        try:
            page = await browser.new_page(viewport={'width': args.width, 'height': args.height}, device_scale_factor=1)
            await page.set_content(html, wait_until='load')
            await page.add_style_tag(content='html{scroll-behavior:auto!important}')
            await page.locator(f'[data-lang="{args.lang}"]').click()
            if args.demo:
                await page.locator('#launch-demo').click()
                await page.frame_locator('#demo-frame').locator('.zone').first.wait_for()
            await page.evaluate('document.fonts.ready')
            await page.evaluate('window.scrollTo(0,0)')
            args.output.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(args.output), full_page=not args.viewport_only)
        finally:
            await browser.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=ROOT / 'preview.html')
    parser.add_argument('--output', type=Path, default=ROOT / 'previews/render.png')
    parser.add_argument('--width', type=int, default=1600)
    parser.add_argument('--height', type=int, default=1000)
    parser.add_argument('--lang', choices=['fr', 'en'], default='en')
    parser.add_argument('--chromium', default=os.environ.get('CHROMIUM'))
    parser.add_argument('--demo', action='store_true', help='Start the local interactive demo before capture')
    parser.add_argument('--viewport-only', action='store_true')
    options = parser.parse_args()
    if options.width < 1 or options.height < 1:
        parser.error('Width and height must be positive')
    try:
        asyncio.run(render(options))
    except Exception as error:
        raise SystemExit(f'Render failed: {error}') from error
