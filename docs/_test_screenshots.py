"""Exercise the screenshot guard on passing and deliberately broken local fixtures."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
        (self.root / "_config.yml").write_text('baseurl: /docs\nexclude: []\n', encoding="utf-8")
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

    def add_page(self, name="second.md", body="No screenshot yet.", metadata="title: Second"):
        page = self.root / name
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"---\n{metadata}\n---\n{body}\n", encoding="utf-8")
        return page

    def include(self, filename="sample-0.png"):
        return f'{{% include screenshot.html file="{filename}" title="Example" caption="Inspect this result." %}}'

    def alias(self, filename="legacy-overview.png"):
        target = self.root / "assets" / filename
        target.write_bytes((self.assets / "sample-0.png").read_bytes())
        return target

    def assert_missing_page(self, body, error=None, metadata="title: Second"):
        self.add_page(body=body, metadata=metadata)
        result = validator.validate()
        self.assertEqual(result["missingScreenshotPages"], ["second.md"])
        self.assertEqual(result["totalPages"], 2)
        self.assertEqual(result["coveredPages"], 1)
        self.assertEqual(result["coveragePercent"], 50.0)
        self.assertIn("Missing screenshot on documentation page: second.md", result["errors"])
        if error:
            self.assertIn(error, "\n".join(result["errors"]))

    def test_complete_synthetic_set_passes(self):
        result = validator.validate()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["linked_screenshots"], 100)
        self.assertEqual(result["totalPages"], 1)
        self.assertEqual(result["coveredPages"], 1)
        self.assertEqual(result["pages"], 1)
        self.assertEqual(result["coveragePercent"], 100.0)
        self.assertEqual(result["missingScreenshotPages"], [])

    def test_second_page_with_valid_include_passes(self):
        self.add_page(body=self.include())
        result = validator.validate()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["totalPages"], 2)
        self.assertEqual(result["coveredPages"], 2)
        self.assertEqual(result["placements"], 101)

    def test_include_with_whitespace_control_and_literal_backticks_passes(self):
        self.add_page(body=self.include().replace('{%', '{%-').replace('%}', '-%}').replace('title="Example"', 'title="`Example`"'))
        result = validator.validate()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["coveredPages"], 2)

    def test_page_without_screenshot_fails(self):
        self.assert_missing_page("# A publishable page without an image")

    def test_indented_or_inline_include_cannot_count_as_valid_figure(self):
        for prefix in ("\t", "    ", "1. ", "Text before "):
            with self.subTest(prefix=prefix):
                self.assert_missing_page(prefix + self.include(), "Screenshot include must start at column one")

    def test_auxiliary_readmes_are_not_exempt(self):
        for name in ("README.md", "assets/README.md"):
            self.add_page(name, metadata="title: Auxiliary\nnav_exclude: true\nsearch_exclude: true\nsitemap: false")
        result = validator.validate()
        self.assertEqual(result["totalPages"], 3)
        self.assertEqual(result["missingScreenshotPages"], ["README.md", "assets/README.md"])
        self.assertTrue(result["errors"])

    def test_config_excluded_pages_are_ignored(self):
        (self.root / "_config.yml").write_text(
            'exclude: [drafts, guides/private, "**/omit-*.md", "guides/**/hidden", "*.scratch.md", "guides/*/skip.md"]\n',
            encoding="utf-8")
        for name in ("drafts/a.md", "guides/private/a.md", "omit-root.md", "guides/deep/omit-child.md",
                     "guides/hidden/a.md", "guides/deep/hidden/a.md", "guides/draft.scratch.md",
                     "guides/deep/skip.md"):
            self.add_page(name, body=self.include("unknown.png"))
        self.add_page("guides/public.md", body=self.include())
        result = validator.validate()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["totalPages"], 2)

    def test_jekyll_reserved_paths_are_ignored(self):
        for name in ("_site/a.md", "_includes/a.md", "_layouts/a.md", "_data/a.md", "_sass/a.md",
                     "_plugins/a.md", ".git/a.md", ".jekyll-cache/a.md", "__pycache__/a.md",
                     "node_modules/a.md", "vendor/bundle/a.md", "guide/_private/a.md", ".hidden.md"):
            self.add_page(name, body=self.include("unknown.png"))
        result = validator.validate()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["totalPages"], 1)

    def test_unpublished_and_no_frontmatter_files_are_ignored(self):
        self.add_page("draft.md", metadata="title: Draft\npublished: false")
        (self.root / "notes.md").write_text("# Not a rendered page\n", encoding="utf-8")
        result = validator.validate()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["totalPages"], 1)

    def test_empty_frontmatter_and_other_page_extensions_are_counted(self):
        self.add_page("empty.md", metadata="")
        self.add_page("long.markdown")
        self.add_page("landing.html")
        result = validator.validate()
        self.assertEqual(result["totalPages"], 4)
        self.assertEqual(result["missingScreenshotPages"], ["empty.md", "landing.html", "long.markdown"])

    def test_frontmatter_image_does_not_cover_page(self):
        self.alias()
        self.assert_missing_page("# Metadata is not a screenshot", metadata="title: Second\nimage: /assets/legacy-overview.png")

    def test_include_in_frontmatter_does_not_cover_page(self):
        self.assert_missing_page("# Body", metadata="title: Second\nexample: |\n  " + self.include())

    def test_social_defaults_and_meta_tag_do_not_cover_page(self):
        self.alias("social-preview.png")
        (self.root / "_config.yml").write_text(
            'exclude: []\ndefaults:\n  - scope: {path: ""}\n    values: {image: /assets/social-preview.png}\n', encoding="utf-8")
        self.assert_missing_page('<meta property="og:image" content="/assets/social-preview.png">')

    def test_deployment_badge_does_not_cover_page(self):
        self.assert_missing_page('[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/)')

    def test_decorative_aliases_do_not_cover_page_even_with_approved_hash(self):
        for name in ("social-preview.png", "logo.png", "deploy-button.png", "status-badge.png", "favicon.png"):
            with self.subTest(name=name):
                self.alias(name)
                self.assert_missing_page(f'![Decoration](/assets/{name})')

    def test_broken_include_does_not_cover_page(self):
        for body, error in ((self.include("unknown.png"), "Unmanifested screenshot"),
                            ('{% include screenshot.html file="sample-0.png" broken %}', "Malformed screenshot include"),
                            ('{% include screenshot.html file="sample-0.png" title="Unclosed %}', "Malformed screenshot include"),
                            (self.include().removesuffix("%}"), "Malformed screenshot include"),
                            ('{% include screenshot.html %}', "Missing title/caption")):
            with self.subTest(body=body):
                self.assert_missing_page(body, error)

    def test_unterminated_include_fails_even_on_an_otherwise_covered_page(self):
        self.page.write_text(self.text + "\n" + self.include().removesuffix("%}"), encoding="utf-8")
        result = validator.validate()
        self.assertEqual(result["missingScreenshotPages"], [])
        self.assertIn("Malformed screenshot include: example.md", result["errors"])

    def test_title_and_caption_required_for_every_include(self):
        for field, replacement in (("title", ""), ("caption", ""), ("title", 'title="  "'), ("caption", 'caption="  "')):
            with self.subTest(field=field, replacement=replacement):
                original = 'title="Example"' if field == "title" else 'caption="Inspect this result."'
                self.assert_missing_page(self.include().replace(original, replacement), "Missing title/caption")

    def test_missing_asset_does_not_cover_page(self):
        (self.assets / "sample-0.png").unlink()
        self.assert_missing_page(self.include(), "Missing screenshot: sample-0.png")

    def test_excluded_asset_does_not_cover_page(self):
        (self.root / "_config.yml").write_text('exclude: [assets/screenshots/sample-0.png]\n', encoding="utf-8")
        self.assert_missing_page(self.include(), "Screenshot excluded from publication")

    def test_changed_asset_does_not_cover_page(self):
        Image.new("RGB", (2, 2), (255, 255, 255)).save(self.assets / "sample-0.png")
        self.assert_missing_page(self.include(), "Changed artifact hash")

    def test_missing_include_template_does_not_cover_pages(self):
        (self.root / "_includes/screenshot.html").unlink()
        result = validator.validate()
        self.assertEqual(result["coveredPages"], 0)
        self.assertEqual(result["missingScreenshotPages"], ["example.md"])
        self.assertIn("Screenshot include template is missing", result["errors"])

    def test_legacy_approved_alias_counts_as_manifest_reference(self):
        self.alias()
        self.page.write_text(self.text.replace(self.include(), ""), encoding="utf-8")
        self.add_page(body='![Legacy overview]({{ site.baseurl }}/assets/legacy-overview.png)')
        result = validator.validate()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["totalPages"], 2)
        self.assertEqual(result["coveredPages"], 2)
        self.assertEqual(result["linked_screenshots"], 100)
        self.assertEqual(result["placements"], 100)

    def test_legacy_alias_url_forms(self):
        self.alias()
        for body in ('![Legacy](/assets/legacy-overview.png "Overview")',
                     '![Legacy]({{site.baseurl}}/assets/legacy-overview.png?size=full#image)',
                     '![Legacy](/docs/assets/legacy-overview.png)',
                     '![Legacy]({{ "/assets/legacy-overview.png" | relative_url }})',
                     '<img alt="Legacy" src="/assets/legacy-overview.png">',
                     '![Legacy](../assets/legacy-overview.png)'):
            with self.subTest(body=body):
                self.add_page("guide/legacy.md", body=body)
                result = validator.validate()
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["coveredPages"], 2)

    def test_unapproved_missing_and_external_legacy_images_do_not_cover_page(self):
        Image.new("RGB", (2, 2), (255, 255, 255)).save(self.root / "assets/unapproved.png")
        self.alias()
        for url in ("/assets/unapproved.png", "/assets/missing.png", "https://example.com/assets/legacy-overview.png",
                    "//example.com/assets/legacy-overview.png", "/assets/screenshots/sample-0.png"):
            with self.subTest(url=url):
                self.assert_missing_page(f'![Image]({url})')

    def test_legacy_alias_requires_existing_valid_manifest_asset(self):
        self.alias()
        (self.assets / "sample-0.png").unlink()
        self.assert_missing_page('![Legacy](/assets/legacy-overview.png)', "Missing screenshot: sample-0.png")

    def test_code_comments_and_plain_links_do_not_cover_page(self):
        self.alias()
        for body in (f'```liquid\n{self.include()}\n```', f'~~~\n{self.include()}\n~~~',
                     f'`{self.include()}`', f'<!-- {self.include()} -->',
                     '{% comment %}' + self.include() + '{% endcomment %}',
                     '{% raw %}' + self.include() + '{% endraw %}',
                     '[Screenshot](/assets/legacy-overview.png)'):
            with self.subTest(body=body):
                self.assert_missing_page(body)

    def test_image_text_in_broken_include_does_not_cover_page(self):
        self.alias()
        self.assert_missing_page(
            self.include("unknown.png").replace('title="Example"', 'title="![Example](/assets/legacy-overview.png)"'),
            "Unmanifested screenshot")

    def test_excluded_page_does_not_supply_asset_linkage(self):
        (self.root / "_config.yml").write_text('exclude: [private]\n', encoding="utf-8")
        self.page.write_text(self.text.replace(self.include(), ""), encoding="utf-8")
        self.add_page("private/only-reference.md", body=self.include())
        self.assertIn("Screenshot not linked from a documentation page: sample-0.png", self.errors())

    def test_malformed_frontmatter_fails_without_hiding_page(self):
        for text in ('---\ntitle: [broken\n---\n', '---\ntitle: Never closed\n', '---\n- not a mapping\n---\n'):
            with self.subTest(text=text):
                (self.root / "second.md").write_text(text + self.include(), encoding="utf-8")
                result = validator.validate()
                self.assertEqual(result["totalPages"], 2)
                self.assertEqual(result["missingScreenshotPages"], ["second.md"])
                self.assertIn("Malformed front matter", "\n".join(result["errors"]))

    def test_missing_or_invalid_config_fails_closed(self):
        config = self.root / "_config.yml"
        config.unlink()
        with self.assertRaises(OSError):
            validator.validate()
        for text in ('exclude: [unterminated', 'exclude: private', 'exclude: [null]', '- not a mapping'):
            with self.subTest(text=text):
                config.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    validator.validate()

    def test_unreadable_directory_fails_closed(self):
        def unreadable_walk(root, *, onerror):
            onerror(PermissionError("Unreadable documentation directory"))
            return iter(())

        with patch.object(validator.os, "walk", side_effect=unreadable_walk):
            with self.assertRaises(PermissionError):
                validator.validate()

    def test_short_and_truncated_pngs_report_errors_without_crashing(self):
        image = self.assets / "sample-0.png"
        original = image.read_bytes()
        for payload in (b"", b"\x89PNG\r\n\x1a\n", original[:20], original[:32], original[:-5], original[:-12]):
            with self.subTest(length=len(payload)):
                image.write_bytes(payload)
                # Matching the hash must not make a truncated PNG count as a valid capture.
                self.entries[0]["sha256"] = hashlib.sha256(payload).hexdigest()
                self.save_manifest()
                self.assert_missing_page(self.include(), "Incomplete or invalid PNG")

    def test_asset_minimum_and_exact_manifest_count_remain_required(self):
        self.entries.pop()
        (self.assets / "sample-99.png").unlink()
        self.save_manifest()
        self.assertIn("at least 100 screenshots and an exact count", self.errors())

    def test_incorrect_manifest_count_fails(self):
        (self.assets / "manifest.json").write_text(json.dumps({"count": 101, "screenshots": self.entries}), encoding="utf-8")
        self.assertIn("at least 100 screenshots and an exact count", self.errors())

    def test_unmanifested_asset_fails(self):
        Image.new("RGB", (2, 2)).save(self.assets / "extra.png")
        self.assertIn("Unmanifested file: extra.png", self.errors())

    def test_manifest_dimensions_are_checked(self):
        self.entries[0]["width"] = 3
        self.save_manifest()
        self.assertIn("Invalid image dimensions", self.errors())

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