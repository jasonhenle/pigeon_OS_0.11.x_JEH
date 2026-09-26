"""The HDMI source tile in settings_pigeon no longer shows a status LED."""

import unittest
import xml.etree.ElementTree as ET

from pigeon.widgets import pigeon_settings as ps
from pigeon.widgets.main_settings import MainSettingsState


class HdmiLedRetiredTests(unittest.TestCase):
    def _root(self):
        return ET.parse(ps.default_pigeon_settings_svg_path()).getroot()

    def _el(self, root, lid):
        return ps._find_by_logical_id(root, lid)

    def test_hdmi_led_hidden_other_leds_kept(self):
        root = self._root()
        ps.apply_pigeon_settings_svg_state(root, MainSettingsState())
        hdmi = self._el(root, "settings_pigeon_08_hdmi_status_icon")
        self.assertIsNotNone(hdmi)
        self.assertEqual(hdmi.get("display"), "none")
        for lid in ("settings_pigeon_06_wifi_status_icon", "settings_pigeon_09_audio_status_icon"):
            el = self._el(root, lid)
            if el is not None:
                self.assertNotEqual(el.get("display"), "none", lid)

    def test_hdmi_is_not_a_status_kind(self):
        self.assertNotIn("hdmi", [k for k, _ in ps._STATUS_ICONS])
        self.assertFalse(hasattr(MainSettingsState(), "pigeon_hdmi_ok"))


if __name__ == "__main__":
    unittest.main()
