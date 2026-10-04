"""Wikidata episode → series fallback (pigeon/wikidata_episode.py), offline."""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

_SYS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SYS_ROOT not in sys.path:
    sys.path.insert(0, _SYS_ROOT)

from pigeon import wikidata_episode as wd  # noqa: E402

# Trimmed from a real ``wbsearchentities`` response for ``Weight Loss``.
_WEIGHT_LOSS_ROWS = [
    {"label": "weight loss", "description": "reduction of the total body mass"},
    {"label": "Weight Loss", "description": "two-part episode of The Office"},
    {"label": "Weight Loss (part 1)", "description": "episode of The Office (S5 E1)"},
    {"label": "Weight Loss (part 2)", "description": "episode of The Office (S5 E2)"},
]


def _fake_search(params: dict[str, str], **_kw) -> dict:
    # Wikidata prefix search returns nothing for ``Weight Loss Part 2``.
    return {"search": _WEIGHT_LOSS_ROWS if params["search"] == "Weight Loss" else []}


class PartSuffixTests(unittest.TestCase):
    def test_split_part_suffix(self) -> None:
        self.assertEqual(wd._split_part_suffix("Weight Loss Part 2"), ("Weight Loss", "2"))
        self.assertEqual(wd._split_part_suffix("Weight Loss (part 2)"), ("Weight Loss", "2"))
        self.assertEqual(wd._split_part_suffix("Weight Loss - Pt. Two"), ("Weight Loss", "2"))
        self.assertEqual(wd._split_part_suffix("Goodbye, Part II"), ("Goodbye", "2"))
        self.assertIsNone(wd._split_part_suffix("Departure"))
        self.assertIsNone(wd._split_part_suffix("Part 2"))


class SeriesLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        wd.clear_wikidata_episode_cache()

    def test_part_title_resolves_via_base_search(self) -> None:
        with mock.patch.object(wd, "_http_json", side_effect=_fake_search):
            self.assertEqual(wd.series_name_from_wikidata_episode_title("Weight Loss Part 2"), "The Office")

    def test_two_part_umbrella_and_unknown_part(self) -> None:
        with mock.patch.object(wd, "_http_json", side_effect=_fake_search):
            self.assertEqual(wd.series_name_from_wikidata_episode_title("Weight Loss"), "The Office")
            self.assertEqual(wd.series_name_from_wikidata_episode_title("Weight Loss Part Three"), None)

    def test_unnameable_episode_row_keeps_title_ambiguous(self) -> None:
        rows = [
            {"id": "Q1", "label": "Pilot", "description": "episode of Welcome to Night Vale published on June 15 2012 (E1)"},
            {"id": "Q2", "label": "Pilot", "description": "episode of Manifest (S1 E1)"},
        ]
        with mock.patch.object(wd, "_http_json", return_value={"search": rows}):
            self.assertIsNone(wd.series_name_from_wikidata_episode_title("Pilot"))


if __name__ == "__main__":
    unittest.main()
