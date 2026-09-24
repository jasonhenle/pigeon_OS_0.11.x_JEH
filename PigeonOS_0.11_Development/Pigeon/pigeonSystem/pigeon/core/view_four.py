"""View 4 (Title Info) text collection.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

import time


def _view_four_text_is_placeholder(s: str) -> bool:
    """Hide rows whose text is empty or ends with ``-`` / ``NONE`` (any case)."""
    t = str(s).strip()
    if not t:
        return True
    while t and t[-1] in {"'", '"'}:
        t = t[:-1].rstrip()
    if not t:
        return True
    up = t.upper()
    return (
        up.endswith("NONE")
        or up.endswith("UNKNOWN")
        or t.endswith("-")
        or t.endswith("—")
    )


def _view_four_display_metadata(*, apple_tv_auto_state) -> dict[str, object] | None:
    """last_metadata with disabled METADATA / HDMI sources removed."""
    md = apple_tv_auto_state.get("last_metadata")
    if not isinstance(md, dict):
        return None
    try:
        from pigeon.source_toggles import redact_disabled_source_fields

        redacted = redact_disabled_source_fields(md)
    except Exception:
        redacted = dict(md)
    return redacted if isinstance(redacted, dict) else dict(md)


def _view_four_metadata_source_on() -> bool:
    try:
        from pigeon.source_toggles import source_enabled

        return bool(source_enabled("metadata"))
    except Exception:
        return True


def _collect_view_four_ocr_lines(md: dict[str, object], *, _view_four_has_value) -> list[str]:
    """HDMI OCR clues for View 4 Title Info; omit empty fields."""
    pairs = (
        ("ocr_status", "ocr.status"),
        ("ocr_title", "ocr.title"),
        ("ocr_lines", "ocr.lines"),
        ("ocr_season", "ocr.season"),
        ("ocr_episode", "ocr.episode"),
        ("ocr_year", "ocr.year"),
        ("ocr_runtime_min", "ocr.runtime_min"),
        ("ocr_extras", "ocr.extras"),
        ("ocr_reason", "ocr.trigger"),
        ("ocr_agrees", "ocr.agrees"),
        ("ocr_at", "ocr.at"),
        ("ocr_capture", "ocr.capture"),
    )
    out: list[str] = []
    for key, label in pairs:
        val: object = md.get(key)
        if key == "ocr_reason":
            mapped = {
                "no_metadata": "no_pyatv_title",
                "watch": "stay_alert",
                "pause": "pause",
                "confirm": "confirm",
            }
            val = mapped.get(str(val or ""), val)
        if key == "ocr_at" and val is not None:
            try:
                val = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(float(val))
                )
            except (TypeError, ValueError, OSError, OverflowError):
                pass
        if not _view_four_has_value(val):
            continue
        if isinstance(val, (list, tuple)):
            items: list[str] = []
            junk_fn = None
            if key == "ocr_lines":
                try:
                    from pigeon.ocr_clues import looks_like_ocr_junk

                    junk_fn = looks_like_ocr_junk
                except Exception:
                    junk_fn = None
            for item in val:
                text = str(item).strip()
                if not _view_four_has_value(text):
                    continue
                if junk_fn is not None and junk_fn(text):
                    continue
                items.append(text)
            if not items:
                continue
            shown = items[:6]
            extra_n = len(items) - len(shown)
            text = ", ".join(shown)
            if extra_n > 0:
                text = f"{text} +{extra_n} more"
            out.append(f"{label}={text}")
            continue
        out.append(f"{label}={val!r}")
    return out


def _collect_view_four_raw_title_lines(*, _collect_view_four_ocr_lines, _tmdb_info_current_and_available, _view_four_display_metadata, _view_four_has_value, _view_four_metadata_source_on, _view_four_text_is_placeholder, apple_tv_auto_state, apple_tv_playback_clock, receiver_telnet_debug_holder, streaming_badge_state) -> list[tuple[str, bool]]:
    """View 4: streaming label, rawTitle + OCR fields that have a value, last TMDb fetch."""
    rows: list[tuple[str, bool]] = []

    def _ln(s: str) -> None:
        if _view_four_text_is_placeholder(s):
            return
        rows.append((s, False))

    lm_rt = _view_four_display_metadata()
    metadata_on = _view_four_metadata_source_on()
    if isinstance(lm_rt, dict):
        for ocr_line in _collect_view_four_ocr_lines(lm_rt):
            _ln(ocr_line)
        try:
            from pigeon.display_confidence import scores_for_metadata
            from pigeon.hdmi_ocr import hdmi_capture_available
            from pigeon.source_toggles import source_enabled
            from pigeon.tmdb_poster import last_trt_comparison

            clk = apple_tv_playback_clock
            advancing = bool(clk.get("has_sync") and clk.get("playing"))
            tmdb_ok = bool(_tmdb_info_current_and_available())
            trt_cmp = last_trt_comparison()

            scores = scores_for_metadata(
                lm_rt,
                position_advancing=advancing,
                tmdb_matches=tmdb_ok,
                hdmi_on=bool(source_enabled("hdmi")),
                hdmi_present=hdmi_capture_available(),
                player_duration_s=trt_cmp.get("player_s"),
                tmdb_runtime_s=trt_cmp.get("tmdb_s"),
            )
            src = str(lm_rt.get("identity_source") or "").strip()
            if src:
                _ln(f"identity.source={src!r}")
            try:
                from pigeon.title_decision import decision_from_metadata

                why = decision_from_metadata(lm_rt) or str(
                    apple_tv_auto_state.get("last_title_decision") or ""
                ).strip()
                if why:
                    _ln(f"title.decision={why}")
            except Exception:
                pass
            pending = str(lm_rt.get("ocr_pending_title") or "").strip()
            if pending:
                _ln(f"identity.pending={pending!r}")
                hits = lm_rt.get("ocr_pending_hits")
                if hits is not None:
                    _ln(f"identity.pending_hits={hits!r}")
            for key in ("identity", "position", "art", "app", "trt"):
                val = scores.get(key)
                if val is None:
                    continue
                _ln(f"confidence.{key}={float(val):.2f}")
            if scores.get("ocr_charge"):
                _ln("ocr.charge=true")
        except Exception:
            pass
    if metadata_on:
        _svc_label = str(streaming_badge_state.get("label") or "").strip()
        _svc_app = (
            str(lm_rt.get("app_name") or "").strip()
            if isinstance(lm_rt, dict)
            else ""
        )
        if _svc_label:
            _ln(f"streamingService={_svc_label!r}")
        elif _svc_app:
            _ln(f"streamingService={_svc_app!r}")

    if not isinstance(lm_rt, dict):
        _ln("rawTitle: (no last_metadata dict)")
    else:
        if metadata_on:
            try:
                from pigeon.raw_title import raw_title_from_metadata_dict

                rt = raw_title_from_metadata_dict(lm_rt)
                if _view_four_has_value(rt.source):
                    _ln(f"rawTitle.source={rt.source!r}")
                for fn in (
                    "raw_title",
                    "raw_series_name",
                    "raw_artist",
                    "raw_album",
                    "raw_episode_title",
                    "raw_query",
                    "season_index",
                    "episode_index",
                    "layer_series_title",
                    "layer_series_number",
                    "layer_episode_number",
                    "layer_episode_title",
                    "media_type_label",
                ):
                    val = getattr(rt, fn, None)
                    if _view_four_has_value(val):
                        _ln(f"rawTitle.{fn}={val!r}")
                if rt.notes:
                    _ln(f"rawTitle.notes={rt.notes!r}")
                sig = rt.training_signature_normalized()
                if sig:
                    _ln(f"rawTitle.training_signature_normalized={sig!r}")
            except Exception as e:
                _ln(f"rawTitle err={e}")
    if metadata_on and isinstance(lm_rt, dict):
        _pp = str(lm_rt.get("prefer_pyatv_media") or "").strip().lower()
        if _pp in ("auto", "tv", "movie"):
            _ln(f"metadata.prefer_pyatv_media={_pp!r}")
        _ip = str(lm_rt.get("inferred_prefer") or "").strip().lower()
        if _ip in ("auto", "tv", "movie"):
            _ln(f"metadata.prefer_tmdb={_ip!r}")
    ocr_guess = (
        str(lm_rt.get("ocr_title") or "").strip().casefold()
        if isinstance(lm_rt, dict)
        else ""
    )

    def _tmdb_row_allowed(q: object) -> bool:
        if metadata_on:
            return True
        t = str(q or "").strip().casefold()
        return bool(t and ocr_guess and (t == ocr_guess or t in ocr_guess or ocr_guess in t))

    _ti = apple_tv_auto_state.get("last_tmdb_fetch_input")
    _tr = apple_tv_auto_state.get("last_tmdb_fetch_refined")
    _tp = apple_tv_auto_state.get("last_tmdb_fetch_prefer")
    if _ti is not None and str(_ti).strip() and _tmdb_row_allowed(_ti):
        _ln(f"tmdbFetch.input_query={str(_ti)!r}")
    if _tr is not None and str(_tr).strip() and _tmdb_row_allowed(_tr):
        _ln(f"tmdbFetch.refined_query={str(_tr)!r}")
    if _tp is not None and str(_tp).strip() and (_tmdb_row_allowed(_ti) or _tmdb_row_allowed(_tr)):
        _ln(f"tmdbFetch.prefer={str(_tp)!r}")
    rx_dbg = receiver_telnet_debug_holder[0] if receiver_telnet_debug_holder else {}
    if isinstance(rx_dbg, dict) and rx_dbg:
        _ln("denonTelnet (debug):")
        for key in ("SI", "MS", "DC", "PS_MULTEQ", "PS_DYNEQ", "PS_DYNVOL", "PS_REFLEV"):
            val = str(rx_dbg.get(key) or "").strip()
            if val:
                _ln(f"  {key}={val!r}")
        raw_blob = str(rx_dbg.get("_raw") or "").strip()
        if raw_blob:
            _ln("  _raw=(see receiver_denon_telnet dump)")
    return rows
