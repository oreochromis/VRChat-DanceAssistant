"""Background-image palette extraction and theme integration."""

import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from dance_ui.palette import palette_from_image
from dance_ui.theme import ThemeStore, contrast_ratio, theme_contrast_warnings


class PaletteTests(unittest.TestCase):
    def test_extracts_coordinated_valid_colors_without_modifying_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "arcade.png"
            image = Image.new("RGB", (80, 60), "#25B7CF")
            for box, color in ((box, color) for box, color in (
                ((0, 0, 30, 30), "#8468D7"),
                ((40, 0, 80, 25), "#F2C94C"),
                ((25, 35, 65, 60), "#E8F8F6"),
            )):
                image.paste(color, box)
            image.save(path)
            original = path.read_bytes()
            values = palette_from_image(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertIn("accent", values)
            self.assertIn("secondary_accent", values)
            self.assertIn("background_gradient", values)
            for key, value in values.items():
                if key in ("background_gradient_opacity", "hero_gradient_opacity"):
                    self.assertGreater(value, 0)
                else:
                    self.assertRegex(value, re.compile(r"^#[0-9A-F]{6}$"), key)
            self.assertGreaterEqual(contrast_ratio(values["text_primary"], values["background_color"]), 4.5)
            store = ThemeStore(root / "appearance")
            store.change(values)
            self.assertEqual(store.active["accent"], values["accent"])
            self.assertEqual(ThemeStore(store.root_dir).active["secondary_accent"], values["secondary_accent"])
            self.assertEqual(theme_contrast_warnings(store.active), [])
            dark_store = ThemeStore(root / "dark-appearance")
            dark_store.select("dark")
            dark_store.change(palette_from_image(path, base="dark"))
            self.assertEqual(theme_contrast_warnings(dark_store.active), [])


if __name__ == "__main__":
    unittest.main()
