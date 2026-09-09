#!/usr/bin/env python3
"""Verify repository sources, generated outputs and local Markdown/asset links."""
import hashlib
import json
import mimetypes
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import rebuild
import scratch
from rebuild import ROOT, build_demo, build_preview, product_sources

WORK = scratch.scratch_directory()


class SiteSources(unittest.TestCase):
    def test_scratch_defaults_outside_site(self):
        self.assertEqual(WORK, rebuild.REPO / 'work/tmp/site')
        self.assertFalse(WORK.is_relative_to(ROOT))
        with tempfile.TemporaryDirectory(dir=WORK) as td, patch.object(scratch, 'REPO', Path(td)):
            expected = Path(td) / 'work/tmp/site'
            self.assertEqual(scratch.scratch_directory(), expected)
            self.assertTrue(expected.is_dir())
            self.assertEqual(scratch.scratch_directory(), expected)

    def test_scratch_rejects_unsafe_paths_before_creation(self):
        for relative in ('work', 'work/tmp', 'work/tmp/site'):
            for kind in ('symlink', 'dangling', 'file'):
                with self.subTest(path=relative, kind=kind), tempfile.TemporaryDirectory(dir=WORK) as td:
                    repo = Path(td) / 'repo'
                    path = repo / relative
                    path.parent.mkdir(parents=True)
                    outside = Path(td) / 'outside'
                    if kind == 'file':
                        path.write_text('preserve me')
                    else:
                        if kind == 'symlink':
                            outside.mkdir()
                        path.symlink_to(outside)
                    with patch.object(scratch, 'REPO', repo), patch.object(Path, 'mkdir') as mkdir:
                        with self.assertRaises(ValueError):
                            scratch.scratch_directory()
                        mkdir.assert_not_called()

    def test_copy_example_refuses_overwrite_before_register(self):
        snippet = unescape(re.search(r'<code id="method-code">(.*?)</code>',
                                    (ROOT / 'index.html').read_text(), re.S)[1])
        self.assertIn('cp -T --update=none-fail', snippet)
        for conflict in ('fresh', 'file', 'directory', 'symlink-file', 'symlink-directory',
                         'dangling', 'sidecar-file', 'sidecar-directory', 'sidecar-dangling'):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory(dir=WORK) as directory:
                work = Path(directory)
                source = work / 'report.pdf'
                target = work / 'report-new.pdf'
                marker = work / 'registered'
                source.write_bytes(b'new synthetic report')
                existing = work / 'existing'
                existing.mkdir()
                (existing / 'sentinel').write_bytes(b'preserve me')
                dest = Path(str(target) + '.json') if conflict.startswith('sidecar-') else target
                if conflict in ('file', 'sidecar-file'):
                    dest.write_bytes(b'existing synthetic result')
                elif conflict in ('directory', 'sidecar-directory'):
                    dest.mkdir()
                elif conflict.startswith('symlink-'):
                    dest.symlink_to(existing if conflict.endswith('directory') else existing / 'sentinel')
                elif conflict in ('dangling', 'sidecar-dangling'):
                    dest.symlink_to(work / 'missing')
                # Stub register: this verifies the shell guard, not backend registration.
                script = 'pasteberth() { touch registered; }\n' + snippet.replace(
                    'ZONE=/repo/atlas/ignoredbygit/exchange', 'ZONE=' + shlex.quote(str(work)))
                result = subprocess.run(['bash', '-c', script], cwd=work, capture_output=True)
                if conflict == 'fresh':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(target.read_bytes(), source.read_bytes())
                    self.assertTrue(marker.exists())
                else:
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(marker.exists(), 'register must not run after a refused copy')
                    if conflict in ('file', 'sidecar-file'):
                        self.assertEqual(dest.read_bytes(), b'existing synthetic result')
                    if conflict.startswith('sidecar-'):
                        self.assertFalse(target.exists())
                    if dest.is_dir():
                        self.assertFalse((dest / 'report.pdf').exists())
                    self.assertEqual((existing / 'sentinel').read_bytes(), b'preserve me')

    def test_build_rejects_escaping_symlinks_before_any_write(self):
        generated = ['demo.html', 'preview.html', 'assets/example-data.js']
        cases = [(path, sync) for sync in (False, True) for path in generated]
        cases += [(path, True) for path in [*rebuild.PRODUCT, 'source-manifest.json']]
        cases += [('assets', False), ('assets/product', True)]
        for path, sync in cases:
            for dangling in (False, True):
                with self.subTest(path=path, sync=sync, dangling=dangling), tempfile.TemporaryDirectory(dir=WORK) as td:
                    fixture = Path(td) / 'site'
                    shutil.copytree(ROOT, fixture, ignore=shutil.ignore_patterns('.venv', 'qa', 'previews', '__pycache__', 'export.*'))
                    target = fixture / path
                    outside = Path(td) / 'outside'
                    target.rename(outside)
                    target.symlink_to(Path(td) / 'missing' if dangling else outside)
                    # Spy on writes so even the unfixed implementation cannot touch the sentinel.
                    with patch.object(rebuild, 'ROOT', fixture), patch.object(Path, 'write_text') as text, patch.object(Path, 'write_bytes') as binary:
                        with self.assertRaises(ValueError):
                            manifest = product_sources(sync)
                            build_demo(manifest['runtime_version'])
                            build_preview()
                        text.assert_not_called()
                        binary.assert_not_called()

    def test_build_ignores_host_mime_database(self):
        paths = [ROOT / p for p in ('demo.html', 'preview.html', 'assets/example-data.js')]
        before = [p.read_bytes() for p in paths]
        empty_db = mimetypes.MimeTypes(filenames=())
        for mapping in empty_db.types_map:
            mapping.clear()
        with patch.object(mimetypes, '_db', empty_db):
            self.assertEqual(mimetypes.guess_type('atlas.xlsx'), (None, None))
            build_demo(product_sources()['runtime_version'])
            build_preview()
        try:
            self.assertEqual(before, [p.read_bytes() for p in paths], 'Host MIME database changed generated bytes')
            examples = json.loads(paths[2].read_text().removeprefix('window.PB_EXAMPLES=').removesuffix(';'))
            self.assertEqual(examples['atlas.xlsx']['mime'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        finally:
            # Restore ordinary outputs when exposing the pre-fix regression.
            build_demo(product_sources()['runtime_version'])
            build_preview()

    def test_publish_allowlist_contains_only_explicit_public_files(self):
        recipe = re.search(r'```sh\n(.*?)\n```', (ROOT / 'DEPLOYMENT.md').read_text(), re.S)[1]
        syntax = subprocess.run(['bash', '-n'], input=recipe, text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        paths = (ROOT / 'publish-files.txt').read_text().splitlines()
        self.assertEqual(len(paths), len(set(paths)))
        for path in paths:
            self.assertNotRegex(path, r'[*?\[\]\\]')
            self.assertNotIn('..', Path(path).parts)
            self.assertNotIn(Path(path).parts[0], ('.git', 'work', 'storage', 'captures'))
            self.assertFalse(any(part in ('.venv', 'qa', 'previews') or part.startswith('export.') for part in Path(path).parts))
            source = ROOT.parent / path
            self.assertTrue(source.is_file(), path)
            self.assertEqual(source.resolve(), source, path)
        self.assertTrue({'site/index.html', 'site/demo.html', 'GUIDE.md',
                         'docs/using-pasteberth.md', 'docs/provisioning.md',
                         'site/DEPLOYMENT.md'}.issubset(paths))

    def test_current_frontend_and_reproducible_build(self):
        manifest = product_sources()
        paths = [ROOT / p for p in ('demo.html', 'preview.html', 'assets/example-data.js')]
        before = [p.read_bytes() for p in paths]
        build_demo(manifest['runtime_version'])
        build_preview()
        self.assertEqual(before, [p.read_bytes() for p in paths])
        demo = paths[0].read_text()
        self.assertIn('v' + manifest['runtime_version'], demo)
        self.assertNotIn('__PASTEBERTH_', demo)
        for name in ('app.js', 'style.css'):
            self.assertIn((ROOT / 'assets/product' / name).read_text(), demo)
        preview = paths[1].read_text()
        self.assertIn('window.PB_DEMO_HTML=', preview)
        self.assertIn((ROOT / 'assets/site.js').read_text(), preview)
        self.assertNotIn('<script src=', preview)

    def test_historical_captures_are_pinned(self):
        manifest = json.loads((ROOT / 'capture-manifest.json').read_text())
        self.assertIn('historical', manifest['status'])
        for path, digest in manifest['captures'].items():
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), digest)

    def test_local_links_and_anchors(self):
        class Links(HTMLParser):
            def __init__(self):
                super().__init__()
                self.links = []
                self.ids = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'id' in attrs:
                    self.ids.append(attrs['id'])
                for key in ('href', 'src', 'data-image'):
                    if key in attrs:
                        self.links.append(attrs[key])

        page = Links()
        page.feed((ROOT / 'index.html').read_text())
        self.assertEqual(len(page.ids), len(set(page.ids)), 'Duplicate HTML IDs')
        for link in page.links:
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue
            if url.path:
                self.assertTrue((ROOT / unquote(url.path)).is_file(), link)
            elif url.fragment:
                self.assertIn(url.fragment, page.ids, link)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SiteSources))
    failed_tests = {getattr(test, 'test_case', test).id() for test, _ in result.failures + result.errors}
    (ROOT / 'qa').mkdir(exist_ok=True)
    report = {
        'scope': 'Source equality, deterministic MIME-independent rebuild, work/tmp/site scratch validation, no writes before symlink validation, historical capture hashes, local links, explicit public allowlist and GNU cp data/sidecar guard (register stubbed)',
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'preview_sha256': hashlib.sha256((ROOT / 'preview.html').read_bytes()).hexdigest(),
        'summary': {'passed': result.testsRun - len(failed_tests), 'failed': len(failed_tests)},
        'failures': [(str(test), detail) for test, detail in result.failures + result.errors],
    }
    (ROOT / 'qa/source-report.json').write_text(json.dumps(report, indent=2) + '\n')
    raise SystemExit(not result.wasSuccessful())
