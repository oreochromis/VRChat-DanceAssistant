"""Appearance persistence tests: no Qt import and no system/cache-service writes."""

import base64
import copy
import json
import random
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from dance_ui.theme import (
    ThemeStore, ThemeError, BUILTIN_THEMES, COLOR_FIELDS, DEFAULT_PREFERENCES,
    MAX_IMAGE_BYTES, ensure_theme_contrast, theme_contrast_warnings,
)


def png(width=2, height=3):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    # Deliberately cap the payload so oversized-header tests stay lightweight.
    raw = (b"\x00" + b"\x28\x5a\xb4" * min(width, 100)) * min(height, 100)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# Valid 2x3 image files generated once with Pillow; tests have no Pillow dependency.
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAADAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDlKKKK+vPDP//Z"
)
WEBP = base64.b64decode("UklGRjwAAABXRUJQVlA4IDAAAADwAQCdASoCAAMAAUAmJaACdLoB+AAETAAA/vDXA/9Js8TZ4mz5JH/xZyMR2XgAAAA=")


class ThemeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Exercise loading an existing profile as well as fresh defaults.
        legacy = self.root / "appearance"
        legacy.mkdir()
        (legacy / "appearance.json").write_text(json.dumps({
            "version": 1, "active_theme": "light",
            "themes": list(BUILTIN_THEMES.values()),
            "preferences": DEFAULT_PREFERENCES,
        }), encoding="utf-8")
        self.store = ThemeStore(self.root / "appearance")

    def source(self, name="wallpaper.png", data=None):
        source = self.root / name
        source.write_bytes(png() if data is None else data)
        return source

    def test_builtin_edits_persist_independently(self):
        self.store.change({"accent": "abc", "radius": 7})
        saved_light = self.store.active["accent"]
        self.store.select("dark")
        self.assertEqual(self.store.active["accent"], BUILTIN_THEMES["dark"]["accent"])
        self.store.change({"accent": "#123456"})
        saved_dark = self.store.active["accent"]
        reopened = ThemeStore(self.store.root_dir)
        self.assertEqual(reopened.active_id, "dark")
        self.assertEqual(reopened.active["accent"], saved_dark)
        reopened.select("light")
        self.assertEqual(reopened.active["accent"], saved_light)
        self.assertEqual(reopened.active["radius"], 7)

    def test_preferences_survive_theme_changes_and_resets(self):
        preferences = {"scale": 125, "density": "compact", "logs_expanded": True, "dismissed_update_version": ""}
        self.store.change_preferences(preferences)
        self.store.select("dark")
        self.store.change({"radius": 18})
        self.store.reset_current()
        expected = {**DEFAULT_PREFERENCES, **preferences}
        self.assertEqual(self.store.preferences, expected)
        self.assertEqual(ThemeStore(self.store.root_dir).preferences, expected)
        self.store.reset_preferences()
        self.assertEqual(self.store.preferences, DEFAULT_PREFERENCES)

    def test_ui_mode_defaults_to_simple_for_fresh_install(self):
        fresh = ThemeStore(self.root / "brand-new")
        self.assertEqual(fresh.preferences["ui_mode"], "simple")

    def test_ui_mode_defaults_to_advanced_for_legacy_profile(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        doc = {"version": 1, "active_theme": "light",
               "themes": list(BUILTIN_THEMES.values()),
               "preferences": {"scale": 100, "density": "standard",
                               "logs_expanded": False, "dismissed_update_version": ""}}
        (legacy / "appearance.json").write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(ThemeStore(legacy).preferences["ui_mode"], "advanced")

    def test_ui_mode_roundtrip_and_validation(self):
        self.store.change_preferences({"ui_mode": "simple"})
        self.assertEqual(ThemeStore(self.store.root_dir).preferences["ui_mode"], "simple")
        self.store.change_preferences({"ui_mode": "advanced"})
        self.assertEqual(self.store.preferences["ui_mode"], "advanced")
        self.store.change_preferences({"ui_mode": "minimal"})
        self.assertEqual(ThemeStore(self.store.root_dir).preferences["ui_mode"], "minimal")
        with self.assertRaises(ThemeError):
            self.store.change_preferences({"ui_mode": "expert"})

    def test_home_style_defaults_persists_and_validates(self):
        self.assertEqual(self.store.preferences["home_style"], "vivid")
        self.assertEqual(theme_contrast_warnings(self.store.active, self.store.preferences), [])
        self.store.change_preferences({"home_style": "simple"})
        self.assertEqual(ThemeStore(self.store.root_dir).preferences["home_style"], "simple")
        self.store.select("dark")
        self.assertEqual(self.store.preferences["home_style"], "simple")
        with self.assertRaises(ThemeError):
            self.store.change_preferences({"home_style": "fancy"})

    def test_additional_builtin_themes_are_readable_and_persist(self):
        self.assertEqual({"miku", "eva01", "jirai", "maimai"} - set(BUILTIN_THEMES), set())
        self.assertEqual({key: BUILTIN_THEMES[key]["name"] for key in ("miku", "eva01", "jirai", "maimai")},
                         {"miku": "葱绿", "eva01": "机甲", "jirai": "阔剑", "maimai": "萌舞"})
        for theme_id in ("miku", "eva01", "jirai", "maimai"):
            with self.subTest(theme=theme_id):
                self.store.select(theme_id)
                background = self.store.background_path()
                self.assertIsNotNone(background)
                self.assertTrue(background.is_file())
                for style in ("simple", "vivid"):
                    self.store.change_preferences({"home_style": style})
                    self.assertEqual(theme_contrast_warnings(self.store.active, self.store.preferences), [])
        self.store.select("maimai")
        self.assertEqual(ThemeStore(self.store.root_dir).active_id, "maimai")

    def test_copy_rename_delete_and_undo_keep_original(self):
        self.store.change({"accent": "#123456"})
        original = self.store.active
        copied = self.store.create(copy_current=True)
        self.store.rename(copied, "我的主题")
        self.store.change({"accent": "#654321"})
        self.store.delete(copied)
        self.assertEqual(self.store.active_id, "light")
        self.assertEqual(self.store.active, original)
        self.assertTrue(self.store.undo())
        self.assertEqual(self.store.active_id, copied)
        self.assertEqual(self.store.active["name"], "我的主题")
        self.assertEqual(self.store.active["accent"], "#654321")
        self.assertEqual(ThemeStore(self.store.root_dir).active_id, copied)

    def test_builtin_delete_and_invalid_rename_do_not_write(self):
        before = self.store.path.read_bytes()
        for operation in (
            lambda: self.store.delete("light"),
            lambda: self.store.rename("light", ""),
            lambda: self.store.rename("light", "深色"),
            lambda: self.store.select("not-a-theme"),
        ):
            with self.assertRaises(ThemeError):
                operation()
            self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(self.store.can_undo)

    def test_normalises_ranges_and_rejects_invalid_transaction(self):
        self.store.change({"radius": 999, "image_blur": -1, "card_opacity": 5, "background_opacity": -3, "border_width": 12})
        self.assertEqual(self.store.active["radius"], 24)
        self.assertEqual(self.store.active["image_blur"], 0)
        self.assertEqual(self.store.active["card_opacity"], 1)
        self.assertEqual(self.store.active["background_opacity"], 0)
        self.assertEqual(self.store.active["border_width"], 6)
        before = self.store.active
        for values in (
            {"accent": "#123456", "radius": float("nan")},
            {"accent": "not-a-color"}, {"image_blur": float("inf")},
            {"unknown_field": 5}, {"image_fit": "invalid"},
        ):
            with self.assertRaises(ThemeError):
                self.store.change(values)
            self.assertEqual(self.store.active, before)
        for values in ({"scale": 120}, {"scale": True}, {"scale": 125.9}, {"density": "tiny"}, {"logs_expanded": "false"}):
            with self.assertRaises(ThemeError):
                self.store.change_preferences(values)

    def test_atomic_write_failure_keeps_memory_disk_and_undo_history(self):
        before = self.store.path.read_bytes()
        with patch("dance_ui.theme.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.change({"accent": "#123456"})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.active["accent"], ensure_theme_contrast(BUILTIN_THEMES["light"], DEFAULT_PREFERENCES)["accent"])
        self.assertFalse(self.store.can_undo)
        self.assertEqual(list(self.store.root_dir.glob(".appearance-*.tmp")), [])

    def test_slider_group_is_one_undo_step(self):
        self.store.change({"accent": "#123456"})
        before_drag = self.store.active
        self.store.begin_edit()
        for value in (0.2, 0.4, 0.8):
            self.store.change({"card_opacity": value})
        self.store.end_edit()
        self.assertEqual(ThemeStore(self.store.root_dir).active["card_opacity"], 0.8)
        self.store.undo()
        self.assertEqual(self.store.active, before_drag)
        self.store.undo()
        self.assertEqual(self.store.active["accent"], ensure_theme_contrast(BUILTIN_THEMES["light"], DEFAULT_PREFERENCES)["accent"])
        self.assertFalse(self.store.undo())

    def test_group_reset_does_not_reset_unrelated_fields(self):
        self.store.change({"accent": "#123456", "card_opacity": 0.3, "radius": 24})
        self.store.reset_group("cards")
        self.assertEqual(self.store.active["accent"], "#123456")
        self.assertEqual(self.store.active["card_opacity"], BUILTIN_THEMES["light"]["card_opacity"])
        self.assertEqual(self.store.active["radius"], BUILTIN_THEMES["light"]["radius"])
        self.store.undo()
        self.assertEqual(self.store.active["card_opacity"], 0.3)

    def test_theme_export_import_roundtrip_excludes_background_reference(self):
        self.store.import_background(self.source())
        self.store.change({"secondary_accent": "#123456", "hero_gradient_opacity": .42,
                           "gradient_direction": "horizontal", "button_text": "#234567"})
        path = self.root / "shared-theme.json"
        self.store.export_theme(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["format"], "vrchat-dance-theme")
        self.assertNotIn("background_image", raw["theme"])
        theme_id = self.store.import_theme(path)
        self.assertEqual(self.store.active_id, theme_id)
        self.assertEqual(self.store.active["secondary_accent"], "#123456")
        self.assertEqual(self.store.active["hero_gradient_opacity"], .42)
        self.assertEqual(self.store.active["gradient_direction"], "horizontal")
        self.assertEqual(self.store.active["button_text"], "#234567")
        self.assertEqual(self.store.active["background_image"], "")
        self.assertEqual(ThemeStore(self.store.root_dir).active["id"], theme_id)

    def test_invalid_theme_import_and_gradient_are_transactional(self):
        path = self.root / "invalid-theme.json"
        path.write_text('{"format":"other"}', encoding="utf-8")
        before = self.store.path.read_bytes()
        with self.assertRaises(ThemeError):
            self.store.import_theme(path)
        with self.assertRaises(ThemeError):
            self.store.change({"gradient_direction": "sideways"})
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_contrast_protection_corrects_instead_of_saving_unreadable_text(self):
        self.assertEqual(theme_contrast_warnings(self.store.active), [])
        self.store.change({"text_primary": "#FFFFFF", "card_background": "#FFFFFF"})
        self.assertNotEqual(self.store.active["text_primary"], "#FFFFFF")
        self.assertEqual(theme_contrast_warnings(self.store.active), [])
        self.assertEqual(theme_contrast_warnings(ThemeStore(self.store.root_dir).active), [])

    def test_all_text_roles_are_checked(self):
        theme = self.store.active
        theme.update({
            "nav_active_text": "#FFFFFF", "nav_active_background": "#FFFFFF", "nav_active_opacity": 1,
            "text_secondary": "#FFFFFF", "card_background": "#FFFFFF",
            "button_text": "#FFFFFF", "button_background": "#FFFFFF",
            "table_header_text": "#FFFFFF", "table_header_background": "#FFFFFF",
            "success_color": "#FFFFFF", "highlight": "#FFFFFF",
            "accent": "#FFFFFF", "primary_button_text": "#FFFFFF",
        })
        warnings = theme_contrast_warnings(theme, {"home_style": "vivid"})
        for label in ("导航选中项文字", "辅助文字（卡片）", "普通按钮文字", "表头文字",
                      "成功状态标签", "排名与高亮数字", "主视觉文字"):
            self.assertIn(label + "与背景对比度偏低", warnings)

    def test_random_extreme_themes_are_corrected_to_readable_combinations(self):
        randomizer = random.Random(20261106)
        for base in ("light", "dark"):
            for case in range(100):
                with self.subTest(base=base, case=case):
                    theme = copy.deepcopy(BUILTIN_THEMES[base])
                    theme.update({key: f"#{randomizer.randrange(0x1000000):06X}" for key in COLOR_FIELDS})
                    corrected = ensure_theme_contrast(theme)
                    self.assertEqual(theme_contrast_warnings(corrected), [])

    def test_returned_values_cannot_mutate_store(self):
        self.store.active["accent"] = "#000000"
        self.store.themes[0]["name"] = "modified"
        self.store.preferences["scale"] = 150
        self.assertEqual(self.store.active["accent"], ensure_theme_contrast(BUILTIN_THEMES["light"], DEFAULT_PREFERENCES)["accent"])
        self.assertEqual(self.store.themes[0]["name"], "浅色")
        self.assertEqual(self.store.preferences["scale"], 100)

    def test_imports_copy_png_jpeg_webp_without_changing_source(self):
        for name, data, extension in (("photo.png", png(), ".png"), ("photo.jpeg", JPEG, ".jpg"), ("photo.webp", WEBP, ".webp")):
            source = self.source(name, data)
            relative = self.store.import_background(source)
            imported = self.store.background_path()
            self.assertEqual(imported.parent, self.store.background_dir)
            self.assertEqual(imported.suffix, extension)
            self.assertEqual(imported.read_bytes(), data)
            self.assertEqual(source.read_bytes(), data)
            self.assertFalse(Path(relative).is_absolute())
            source.unlink()
            self.assertTrue(imported.is_file())

    def test_image_copy_is_shared_and_survives_theme_deletion(self):
        source = self.source()
        relative = self.store.import_background(source)
        copied = self.store.create(copy_current=True)
        self.assertEqual(self.store.active["background_image"], relative)
        self.store.delete(copied)
        image = self.store.background_path()
        self.assertTrue(image.is_file())
        self.assertEqual(image.read_bytes(), source.read_bytes())
        self.store.undo()
        self.assertTrue(self.store.background_path().is_file())
        self.assertEqual(self.store.import_background(source), relative)
        self.assertEqual(len(list(self.store.background_dir.glob("*.png"))), 1)

    def test_missing_image_and_external_paths_fall_back(self):
        self.store.import_background(self.source())
        self.store.background_path().unlink()
        self.assertIsNone(self.store.background_path())
        self.assertIsNone(ThemeStore(self.store.root_dir).background_path())
        for path in ("../outside.png", "/tmp/outside.png", "backgrounds/../outside.png"):
            with self.assertRaises(ThemeError):
                self.store.change({"background_image": path})

    def test_rejects_invalid_oversized_and_incomplete_images(self):
        invalid = self.source("invalid.png", b"not a PNG")
        truncated = self.source("truncated.png", png()[:-10])
        dimensions = self.source("huge.png", png(20000, 1))
        pixels = self.source("pixels.png", png(8000, 8000))
        oversized = self.root / "large.jpg"
        with oversized.open("wb") as handle:
            handle.truncate(MAX_IMAGE_BYTES + 1)
        for source in (invalid, truncated, dimensions, pixels, oversized):
            with self.assertRaises(ThemeError):
                self.store.import_background(source)
        self.assertEqual(self.store.active["background_image"], "")
        self.assertEqual(list(self.store.background_dir.iterdir()), [])

    def test_corrupt_document_is_preserved_before_recovery(self):
        broken = b'{"version": 1, invalid'
        self.store.path.write_bytes(broken)
        recovered = ThemeStore(self.store.root_dir)
        self.assertEqual(recovered.active, ensure_theme_contrast(BUILTIN_THEMES["light"], DEFAULT_PREFERENCES))
        backups = list(self.store.root_dir.glob("appearance.corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), broken)
        self.assertTrue(recovered.warnings)
        json.loads(self.store.path.read_text(encoding="utf-8"))

    def test_invalid_document_values_recover_without_global_side_effects(self):
        raw = json.loads(self.store.path.read_text(encoding="utf-8"))
        raw["themes"][0]["accent"] = "broken"
        raw["themes"][0]["radius"] = 1000
        raw["themes"].append({"id": "custom", "name": "custom", "base": [], "accent": "123456"})
        raw["active_theme"] = {}
        raw["preferences"] = {"scale": 999, "density": "compact", "logs_expanded": "false"}
        self.store.path.write_text(json.dumps(raw), encoding="utf-8")
        recovered = ThemeStore(self.store.root_dir)
        self.assertEqual(recovered.active["accent"], ensure_theme_contrast(BUILTIN_THEMES["light"], DEFAULT_PREFERENCES)["accent"])
        self.assertEqual(recovered.active["radius"], 24)
        self.assertEqual(recovered.preferences, {"scale": 100, "density": "compact", "logs_expanded": False,
                                                 "dismissed_update_version": "", "ui_mode": "advanced",
                                                 "home_style": "vivid"})  # 老存档无 ui_mode 键 -> 高级
        self.assertEqual(recovered.active_id, "light")
        independent = ThemeStore(self.root / "another-store")
        self.assertEqual(independent.preferences, DEFAULT_PREFERENCES)
        self.assertEqual(independent.active, ensure_theme_contrast(BUILTIN_THEMES["light"], DEFAULT_PREFERENCES))


if __name__ == "__main__":
    unittest.main()
