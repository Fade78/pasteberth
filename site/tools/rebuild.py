#!/usr/bin/env python3
"""Rebuild the offline demo and self-contained preview using only the stdlib.

Run after editing index.html, assets/site.*, assets/demo-adapter.js, or examples.
The upstream frontend files are preserved; the local adapter is separate.
"""
from __future__ import annotations
import base64
import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
PRODUCT = {
    'assets/product/app.js': 'PasteBerth/runtime/static/app.js',
    'assets/product/style.css': 'PasteBerth/runtime/static/style.css',
    'assets/product/favicon.svg': 'PasteBerth/runtime/static/favicon.svg',
    'assets/product/index.template.html': 'PasteBerth/runtime/templates/index.html',
}
GENERATED = ('demo.html', 'assets/example-data.js', 'preview.html')
MIME_TYPES = {
    '.css': 'text/css', '.js': 'text/javascript', '.html': 'text/html',
    '.svg': 'image/svg+xml', '.png': 'image/png', '.txt': 'text/plain',
    '.csv': 'text/csv', '.pdf': 'application/pdf', '.json': 'application/json',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

def validate_destinations(sync: bool = False) -> None:
    # Check the complete write set before starting, including the later build stages.
    paths = (*GENERATED, *PRODUCT, 'source-manifest.json') if sync else GENERATED
    for relative in paths:
        path = ROOT / relative
        if not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError(f'Output escapes site/: {relative}')
        for component in (path, *path.parents):
            if component == ROOT:
                break
            if component.is_symlink():
                raise ValueError(f'Symlinked output path: {relative}')
        if not path.parent.is_dir() or (path.exists() and not path.is_file()):
            raise ValueError(f'Output requires a regular file in an existing directory: {relative}')

def product_sources(sync: bool = False) -> dict:
    validate_destinations(sync)
    version = re.search(r'__version__ = "([^"]+)"',
                        (REPO / 'PasteBerth/runtime/__init__.py').read_text())[1]
    if tomllib.loads((REPO / 'pyproject.toml').read_text())['project']['version'] != version:
        raise ValueError('Runtime and package versions disagree')
    manifest_path = ROOT / 'source-manifest.json'
    if sync:
        files = []
        for target, source in PRODUCT.items():
            data = (REPO / source).read_bytes()
            (ROOT / target).write_bytes(data)
            files.append({'file': target, 'source': source,
                          'sha256': hashlib.sha256(data).hexdigest()})
        manifest = {'site_version': '2.1', 'runtime_version': version,
                    'source_commit': subprocess.check_output(
                        ['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
                    'note': 'Commit is context; file hashes pin the actual working-tree bytes.',
                    'frontend': files}
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
    manifest = json.loads(manifest_path.read_text())
    if manifest['runtime_version'] != version:
        raise ValueError('Runtime changed; review site claims, then use --sync-product')
    if {f['file']: f['source'] for f in manifest['frontend']} != PRODUCT:
        raise ValueError('Frontend manifest is incomplete')
    for entry in manifest['frontend']:
        data = (ROOT / entry['file']).read_bytes()
        if data != (REPO / entry['source']).read_bytes() or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError(f"Frontend drift: {entry['file']}; review then use --sync-product")
    return manifest

def data_url(path: Path) -> str:
    mime = MIME_TYPES.get(path.suffix.lower(), 'application/octet-stream')
    return f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode('ascii')

def embedded_json(value: object) -> str:
    # Never let a filename or document terminate an inline script element.
    return json.dumps(value, ensure_ascii=False).replace('<', '\\u003c')

def build_demo(version: str) -> None:
    validate_destinations()
    seed = {'overview': json.loads((ROOT / 'tools/demo-seed.json').read_text()), 'files': {}}
    for path in sorted((ROOT / 'assets/examples').iterdir()):
        if path.is_file():
            seed['files'][path.name] = {
                'mime': MIME_TYPES.get(path.suffix.lower(), 'application/octet-stream'),
                'base64': base64.b64encode(path.read_bytes()).decode('ascii'),
            }
    html = (ROOT / 'assets/product/index.template.html').read_text(encoding='utf-8')
    html = html.replace('__PASTEBERTH_URL_PREFIX__', '').replace('__PASTEBERTH_VERSION__', version)
    csp = ("default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; "
           "script-src 'unsafe-inline'; connect-src 'none'; object-src 'none'; "
           "base-uri 'none'; form-action 'none'")
    html = html.replace('<title>Pasteberth</title>',
        '<title>Pasteberth — local website demo</title><meta name="robots" content="noindex">'
        + '<meta http-equiv="Content-Security-Policy" content="' + csp.replace("'", '&#39;') + '">')
    html = html.replace('<link rel="stylesheet" href="/static/style.css">',
        '<style>' + (ROOT / 'assets/product/style.css').read_text() + '</style>')
    html = html.replace('/static/favicon.svg', data_url(ROOT / 'assets/product/favicon.svg'))
    scripts = '<script>window.PB_DEMO_SEED=' + embedded_json(seed) + ';</script>'
    for name in ['assets/demo-adapter.js', 'assets/product/app.js']:
        scripts += '<script>' + (ROOT / name).read_text() + '</script>'
    html = html.replace('<script src="/static/app.js"></script>', scripts)
    (ROOT / 'demo.html').write_text(html, encoding='utf-8')
    (ROOT / 'assets/example-data.js').write_text('window.PB_EXAMPLES=' + embedded_json(seed['files']) + ';', encoding='utf-8')

def build_preview() -> None:
    validate_destinations()
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    html = html.replace('<link rel="stylesheet" href="assets/site.css">',
        '<style>' + (ROOT / 'assets/site.css').read_text() + '</style>')
    html = re.sub(r'<script src="assets/example-data.js">\s*</script>',
        lambda _: '<script>' + (ROOT / 'assets/example-data.js').read_text() + '</script>', html)
    html = re.sub(r'<script src="assets/site.js">\s*</script>',
        lambda _: '<script>window.PB_DEMO_HTML=' + embedded_json((ROOT / 'demo.html').read_text())
        + ';</script><script>' + (ROOT / 'assets/site.js').read_text() + '</script>', html)
    def inline(match: re.Match[str]) -> str:
        attr, relative = match.groups()
        path = ROOT / relative
        return f'{attr}="{data_url(path)}"' if path.is_file() else match.group(0)
    html = re.sub(r'(src|href|data-image)="(assets/[^\"]+)"', inline, html)
    html = html.replace('href="LICENSE-Pasteberth.txt"',
        'download="LICENSE-Pasteberth.txt" href="' + data_url(ROOT / 'LICENSE-Pasteberth.txt') + '"')
    (ROOT / 'preview.html').write_text(html, encoding='utf-8')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sync-product', action='store_true',
                        help='Refresh frontend copies and hashes from this repository after review')
    args = parser.parse_args()
    try:
        manifest = product_sources(args.sync_product)
        build_demo(manifest['runtime_version'])
        build_preview()
    except (OSError, ValueError) as error:
        raise SystemExit(f'Build failed: {error}') from error
    print('Built demo.html, assets/example-data.js and preview.html')
