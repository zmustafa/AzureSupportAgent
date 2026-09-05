"""Exercise the screenshot guard on passing and deliberately broken local fixtures."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image, PngImagePlugin

SPEC = importlib.util.spec_from_file_location("screenshot_validation", Path(__file__).with_name("_validate_screenshots.py"))
assert SPEC is not None and SPEC.loader is not None
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class ScreenshotValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets/screenshots"
        self.assets.mkdir(parents=True)
        (self.root / "_includes").mkdir()
        (self.root / "_includes/screenshot.html").write_text("<figure></figure>", encoding="utf-8")
        self.entries = []
        for i in range(100):
            image = self.assets / f"sample-{i}.png"
            Image.new("RGB", (2, 2), (i, 80, 120)).save(image)
            self.entries.append({"file": image.name, "width": 2, "height": 2,
                                 "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                                 "source": "synthetic-local-capture"})
        self.save_manifest()
        self.page = self.root / "example.md"
        self.text = '---\ntitle: Example\n---\n' + '\n'.join(
            '{% include screenshot.html file="' + e["file"] + '" title="Example" caption="Inspect this result." %}'
            for e in self.entries)
        self.page.write_text(self.text, encoding="utf-8")
        self.old_root, self.old_assets = validator.ROOT, validator.ASSETS
        validator.ROOT, validator.ASSETS = self.root, self.assets

    def tearDown(self):
        validator.ROOT, validator.ASSETS = self.old_root, self.old_assets
        self.temp.cleanup()

    def save_manifest(self):
        (self.assets / "manifest.json").write_text(json.dumps({"count": len(self.entries), "screenshots": self.entries}), encoding="utf-8")

    def errors(self):
        return "\n".join(validator.validate()["errors"])

    def test_complete_synthetic_set_passes(self):
        result = validator.validate()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["linked_screenshots"], 100)

    def test_missing_asset_fails(self):
        (self.assets / "sample-0.png").unlink()
        self.assertIn("Missing screenshot", self.errors())

    def test_unlinked_asset_fails(self):
        self.page.write_text(self.text.replace('file="sample-0.png"', 'file="sample-1.png"'), encoding="utf-8")
        self.assertIn("not linked", self.errors())

    def test_missing_caption_fails(self):
        self.page.write_text(self.text.replace('caption="Inspect this result."', '', 1), encoding="utf-8")
        self.assertIn("Missing title/caption", self.errors())

    def test_unmanifested_include_fails(self):
        self.page.write_text(self.text.replace('sample-0.png', 'unknown.png'), encoding="utf-8")
        self.assertIn("Unmanifested screenshot", self.errors())

    def test_changed_pixels_fail_hash(self):
        Image.new("RGB", (2, 2), (255, 255, 255)).save(self.assets / "sample-0.png")
        self.assertIn("Changed artifact hash", self.errors())

    def test_duplicate_files_fail(self):
        (self.assets / "sample-1.png").write_bytes((self.assets / "sample-0.png").read_bytes())
        self.entries[1]["sha256"] = self.entries[0]["sha256"]
        self.save_manifest()
        self.assertIn("Duplicate image", self.errors())

    def test_metadata_is_rejected(self):
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Author", "Synthetic canary")
        Image.new("RGB", (2, 2), (0, 80, 120)).save(self.assets / "sample-0.png", pnginfo=metadata)
        self.assertIn("Unnecessary image metadata", self.errors())

    def test_unknown_provenance_is_rejected(self):
        self.entries[0]["source"] = "unknown"
        self.save_manifest()
        self.assertIn("Unknown provenance", self.errors())


if __name__ == "__main__":
    unittest.main()