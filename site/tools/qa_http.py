#!/usr/bin/env python3
"""Smoke-test actual static HTTP URLs, Markdown and the modular demo on loopback.

Starts a read-only server rooted at the repository on an ephemeral loopback port.
No daemon, product configuration or product data is created or changed.
"""
import asyncio
import functools
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
report = {'scope': 'Loopback static HTTP, root and mounted paths; no Pasteberth daemon',
          'tests': [], 'errors': [], 'requests': []}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/mounted/'):
            self.path = self.path[len('/mounted'):]
        super().do_GET()

    def log_message(self, *_):
        pass


def record(name, ok, detail=None):
    report['tests'].append({'name': name, 'passed': bool(ok), 'detail': detail})
    print(('PASS ' if ok else 'FAIL ') + name, flush=True)


async def run(port):
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get('CHROMIUM'), headless=True)
        report['browser'] = browser.version
        try:
            for mount in ('', '/mounted'):
                origin = f'http://127.0.0.1:{port}'
                url = origin + mount + '/site/index.html'
                context = await browser.new_context(locale='fr-FR')
                page = await context.new_page()
                page.on('pageerror', lambda e: report['errors'].append(str(e)))
                page.on('request', lambda r: report['requests'].append(r.url))
                await page.goto(url)
                record(mount + ' first visit defaults to EN', await page.locator('html').get_attribute('lang') == 'en')
                await page.locator('[data-lang="fr"]').click()
                await page.reload()
                record(mount + ' real Storage persists FR', await page.locator('html').get_attribute('lang') == 'fr')
                await page.goto(url + '?lang=en')
                record(mount + ' real query overrides Storage', await page.locator('html').get_attribute('lang') == 'en')
                links = await page.locator('#documentation a[href$=".md"], #documentation a[href$=".py"]').evaluate_all(
                    '(links) => links.map(a => ({href:a.getAttribute("href"), url:a.href}))')
                for link in links:
                    if link['url'].startswith(origin):
                        response = await context.request.get(link['url'])
                        expected = (ROOT / link['href']).read_bytes()
                        record(mount + ' Documentation bytes ' + link['href'],
                               response.status == 200 and await response.body() == expected,
                               {'status': response.status, 'content_type': response.headers.get('content-type')})
                await page.locator('#launch-demo').click()
                frame = page.frame_locator('#demo-frame')
                await frame.locator('.zone').first.wait_for()
                version = json.loads((ROOT / 'source-manifest.json').read_text())['runtime_version']
                record(mount + ' modular demo uses current frontend',
                       await frame.locator('.brand-version').inner_text() == 'v' + version)
                record(mount + ' product UI stays English', await frame.locator('html').get_attribute('lang') == 'en')
                for width in (390, 1024, 1600):
                    await page.set_viewport_size({'width': width, 'height': 900})
                    record(mount + f' HTTP layout {width}', await page.evaluate(
                        'document.documentElement.scrollWidth <= innerWidth'))
                await page.set_viewport_size({'width': 390, 'height': 844})
                await page.locator('#menu-toggle').click()
                await page.locator('#mobile-nav a[href="#documentation"]').click()
                record(mount + ' mobile documentation navigation',
                       page.url.endswith('#documentation') and not await page.locator('#mobile-nav').is_visible())
                await context.close()
            record('No JS errors over HTTP', not report['errors'], report['errors'])
            record('No remote or daemon API requests', all(
                u.startswith(f'http://127.0.0.1:{port}/') and '/api/' not in u
                for u in report['requests'] if u.startswith(('http:', 'https:'))))
        finally:
            await browser.close()


if __name__ == '__main__':
    (ROOT / 'qa').mkdir(exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(REPO)))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        asyncio.run(run(server.server_port))
    except Exception as error:
        record('HTTP test runner completed', False, str(error))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    report['index_sha256'] = hashlib.sha256((ROOT / 'index.html').read_bytes()).hexdigest()
    report['summary'] = {'passed': sum(t['passed'] for t in report['tests']),
                         'failed': sum(not t['passed'] for t in report['tests'])}
    (ROOT / 'qa/http-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(report['summary'])
    raise SystemExit(bool(report['summary']['failed']))
