"""
Resolve an episode-only title to its parent TV series via Wikidata.

Used when Apple TV / streamers (Peacock, etc.) report an episode line with no
series name, and the local kids episode index / hints do not cover it.

Requires an unambiguous Wikidata hit: exact episode label match and exactly one
``episode of …`` candidate (so generic titles like ``Pilot`` stay unresolved).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_UA = "Pigeon/0.9 (episode-series; local media display)"
_API = "https://www.wikidata.org/w/api.php"
_EPISODE_DESC_RE = re.compile(
    r"(?i)^(?:(?:two|multi)-part )?(?:tv |television )?episode of\b"
)
_SERIES_FROM_DESC_RE = re.compile(
    r"(?i)^(?:(?:two|multi)-part )?(?:tv |television )?episode of (.+?)(?:\s*\(|$)"
)
# Trailing multi-part marker: ``Part 2``, ``(part 2)``, ``Pt. II``, ``- Part Two``.
_PART_SUFFIX_RE = re.compile(
    r"[\s,:\-–—]*[\(\[]?\s*\b(?:part|pt\.?)\s*(\d+|one|two|three|four|five|i{1,3}|iv|v)\s*[\)\]]?\s*$",
    re.IGNORECASE,
)
_PART_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5",
}

# In-process cache: normalized episode title → series name or None (negative).
_cache: dict[str, str | None] = {}


def _norm(s: str) -> str:
    try:
        from pigeon.tmdb_poster import _norm_query

        return _norm_query(s)
    except Exception:
        t = re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
        return re.sub(r"\s+", " ", t)


def _http_json(params: dict[str, str], *, timeout_s: float = 12.0) -> dict[str, Any]:
    url = _API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _series_from_episode_description(desc: str) -> str | None:
    m = _SERIES_FROM_DESC_RE.match((desc or "").strip())
    if not m:
        return None
    name = m.group(1).strip().rstrip(".")
    # Reject descriptions that glued extra prose onto the series name.
    if not name or re.search(r"(?i)\bpublished on\b", name) or len(name) > 80:
        return None
    return name


def _split_part_suffix(title: str) -> tuple[str, str] | None:
    """``Weight Loss Part 2`` / ``Weight Loss (Pt. Two)`` → ``("Weight Loss", "2")``."""
    m = _PART_SUFFIX_RE.search(title or "")
    if not m:
        return None
    base = title[: m.start()].strip(" ,:-–—")
    if not base:
        return None
    num = m.group(1).lower()
    return base, _PART_WORDS.get(num, num)


def _episode_label_key(label: str) -> str:
    """Normalized label with any multi-part suffix canonicalized to ``part N``."""
    split = _split_part_suffix(label)
    if split is None:
        return _norm(label)
    base, num = split
    return f"{_norm(base)} part {num}"


def _search_exact_episode_series(episode_title: str) -> list[str]:
    """Return candidate series names for exact-label episode hits."""
    # Wikidata labels multi-part episodes ``Weight Loss (part 2)``; its prefix search finds
    # nothing for ``Weight Loss Part 2``, so also search the base title.
    searches = [(episode_title, "10")]
    split = _split_part_suffix(episode_title)
    if split is not None:
        searches.append((split[0], "20"))
    rows: list[Any] = []
    for text, limit in searches:
        data = _http_json(
            {
                "action": "wbsearchentities",
                "search": text,
                "language": "en",
                "format": "json",
                "limit": limit,
                "type": "item",
            }
        )
        rows.extend(data.get("search") or [])
    want = _episode_label_key(episode_title)
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "")
        if _episode_label_key(label) != want:
            continue
        desc = str(row.get("description") or "").strip()
        if not _EPISODE_DESC_RE.match(desc):
            continue
        series = _series_from_episode_description(desc)
        # An episode of a series we cannot name still makes the title ambiguous.
        key = _norm(series) if series else f"?{row.get('id') or len(out)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(series or "")
    return out


def series_name_from_wikidata_episode_title(episode_title: str | None) -> str | None:
    """
    Return the parent series name for an unambiguous episode title, or ``None``.

    Ambiguous titles (multiple Wikidata TV episodes with the same name) return
    ``None`` rather than guessing.
    """
    t = (episode_title or "").strip()
    if not t or len(t) < 2:
        return None
    key = _norm(t)
    if not key:
        return None
    if key in _cache:
        return _cache[key]

    try:
        candidates = _search_exact_episode_series(t)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        ValueError,
        KeyError,
    ):
        # Transient network/rate-limit failures: do not cache a negative.
        return None

    # Exactly one distinct series → use it; 0 or 2+ → unresolved (cache either way).
    series = (candidates[0] or None) if len(candidates) == 1 else None
    _cache[key] = series
    return series


def clear_wikidata_episode_cache() -> None:
    """For tests: drop in-process Wikidata episode→series cache."""
    _cache.clear()
