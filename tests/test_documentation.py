"""Exercise documentation paths and selected examples without touching live zones."""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PasteBerth.runtime import __version__
from PasteBerth.runtime.config import load_config, resolve_group_zone_ids
from PasteBerth.runtime.zone_collection import discover_zone_collections, resolve_collection_members


ROOT = Path(__file__).resolve().parents[1]


def markdown_without_fences(path):
    return re.sub(r"^(`{3,}|~{3,}).*?^\1[^\n]*$", "", path.read_text(encoding="utf-8"),
                  flags=re.MULTILINE | re.DOTALL)


def anchors(path):
    text = markdown_without_fences(path)
    counts = {}
    result = set(re.findall(r'<(?:a|h[1-6])\b[^>]*(?:id|name)="([^"]+)"', text))
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.MULTILINE):
        heading = re.sub(r"<[^>]*>", "", heading)
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = counts.get(slug, 0)
        result.add(slug + (f"-{count}" if count else ""))
        counts[slug] = count + 1
    return result


class TestDocumentation(unittest.TestCase):
    def test_markdown_links_and_fragments(self):
        pages = [ROOT / "README.md", ROOT / "GUIDE.md", ROOT / "PasteBerth/support/README.md"]
        pages += sorted((ROOT / "docs").rglob("*.md"))
        pages += sorted((ROOT / "site").glob("*.md"))
        checked = 0
        for page in pages:
            text = markdown_without_fences(page)
            links = re.findall(r"\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)", text)
            links += re.findall(r'^\s*\[[^\]]+\]:\s+(\S+)', text, re.MULTILINE)
            for link in links:
                url = urlsplit(link.strip("<>"))
                if url.scheme or url.netloc:
                    continue
                target = (page.parent / unquote(url.path)).resolve() if url.path else page
                with self.subTest(page=str(page.relative_to(ROOT)), link=link):
                    self.assertTrue(target.is_relative_to(ROOT), "Link escapes repository")
                    self.assertTrue(target.exists(), "Missing link target")
                    if target.suffix == ".md" and url.fragment:
                        self.assertIn(unquote(url.fragment), anchors(target), "Missing heading")
                checked += 1
        self.assertGreater(checked, 200)

    def test_versioned_entry_points(self):
        package = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(package["project"]["version"], __version__)
        for relative in ("README.md", "GUIDE.md", "docs/deployment.md"):
            with self.subTest(page=relative):
                self.assertIn(__version__, (ROOT / relative).read_text())
        readme = (ROOT / "README.md").read_text()
        self.assertIn(f"git clone --branch v{__version__} --depth 1 ", readme)

    def test_provisioning_fragment_discovers_project_not_foreign_file(self):
        document = (ROOT / "docs/provisioning.md").read_text()
        fragment = re.search(r"```toml\n(.*?)\n```", document, re.DOTALL)[1]
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary).resolve()
            base = work / "workspaces"
            zone = base / "alpha/work/exchange"
            zone.mkdir(parents=True)
            (zone / "foreign.txt").write_text("Not registered\n")
            config = work / "config.toml"
            config.write_text(fragment.replace('"/srv/workspaces"', '"' + base.as_posix() + '"'))
            cfg = load_config(config)
            candidates, diagnostics = discover_zone_collections(cfg.zone_collections)
            self.assertEqual(diagnostics, [])
            self.assertEqual([candidate.zone.id for candidate in candidates], ["alpha-work-exchange"])
            self.assertEqual(candidates[0].zone.label, "alpha")
            self.assertEqual(candidates[0].zone.retain, 10)
            zones = {candidate.zone.id: candidate.zone for candidate in candidates}
            # Group matching includes collection membership in the runtime registry.
            members = resolve_collection_members(candidates, zones)
            self.assertEqual(resolve_group_zone_ids(cfg.groups, zones, members),
                             {"Workspaces": ("alpha-work-exchange",)})
            self.assertFalse((zone / "foreign.txt.json").exists())
            (zone / "nested").mkdir()
            candidates, diagnostics = discover_zone_collections(cfg.zone_collections)
            self.assertEqual(candidates, [])
            self.assertTrue(any("subdirectory" in message for message in diagnostics))

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("bash"), "Bash/Linux recipe")
    def test_registration_recipe_refuses_collisions_and_preserves_data(self):
        document = (ROOT / "docs/recipes/register-file.md").read_text()
        snippet = re.search(r"```sh\n(.*?)\n```", document, re.DOTALL)[1]
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary).resolve()
            zone = work / "zone"
            zone.mkdir()
            target = zone / "report.pdf"
            source = work / "report.pdf"
            source.write_bytes(b"Completed synthetic report\n")
            config = work / "config.toml"
            config.write_text(f'[[zones]]\nid = "test"\nlabel = "Test"\ndirectory = "{zone}"\nretain = 1\n')
            snippet = re.sub(r"^target=.*$", "target=" + shlex.quote(str(target)), snippet, flags=re.MULTILINE)
            snippet = snippet.replace("/absolute/path/config.toml", shlex.quote(str(config)))
            snippet = snippet.replace("pasteberth register", shlex.join([sys.executable, "-m", "PasteBerth.runtime", "register"]))
            env = {**os.environ, "PYTHONPATH": str(ROOT)}
            for collision in ("file", "directory", "sidecar", "dangling-sidecar"):
                with self.subTest(collision=collision):
                    sidecar = target.with_name(target.name + ".json")
                    if collision == "file":
                        target.write_bytes(b"Existing result")
                    elif collision == "directory":
                        target.mkdir()
                    elif collision == "sidecar":
                        sidecar.write_text("Existing metadata")
                    else:
                        sidecar.symlink_to(work / "absent")
                    result = subprocess.run(["bash", "-c", snippet], cwd=work, env=env, capture_output=True, timeout=30)
                    self.assertNotEqual(result.returncode, 0)
                    if collision == "file":
                        self.assertEqual(target.read_bytes(), b"Existing result")
                        target.unlink()
                    elif collision == "directory":
                        self.assertEqual(list(target.iterdir()), [])
                        target.rmdir()
                    else:
                        self.assertFalse(target.exists())
                        sidecar.unlink()
            result = subprocess.run(["bash", "-c", snippet], cwd=work, env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertTrue(target.with_name(target.name + ".json").is_file())
            before = target.stat()
            result = subprocess.run([sys.executable, "-m", "PasteBerth.runtime", "register", "--config", str(config), str(target)],
                                    cwd=work, env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            after = target.stat()
            self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
