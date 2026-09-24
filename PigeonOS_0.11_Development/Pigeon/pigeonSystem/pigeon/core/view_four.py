"""View 4 (Title Info) text collection.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations


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
    """Copy of last_metadata for View 4 / the [4] inspector."""
    md = apple_tv_auto_state.get("last_metadata")
    if not isinstance(md, dict):
        return None
    return dict(md)


def _collect_view_four_raw_title_lines(*, _tmdb_info_current_and_available, _view_four_display_metadata, _view_four_has_value, _view_four_text_is_placeholder, apple_tv_auto_state, apple_tv_playback_clock, receiver_telnet_debug_holder, streaming_badge_state) -> list[tuple[str, bool]]:
    """View 4: streaming label, rawTitle fields that have a value, last TMDb fetch."""
    rows: list[tuple[str, bool]] = []

    def _ln(s: str) -> None:
        if _view_four_text_is_placeholder(s):
            return
        rows.append((s, False))

    lm_rt = _view_four_display_metadata()
    if isinstance(lm_rt, dict):
        try:
            from pigeon.display_confidence import scores_for_metadata
            from pigeon.hdmi_capture import hdmi_capture_available
            from pigeon.tmdb_poster import last_trt_comparison

            clk = apple_tv_playback_clock
            advancing = bool(clk.get("has_sync") and clk.get("playing"))
            tmdb_ok = bool(_tmdb_info_current_and_available())
            trt_cmp = last_trt_comparison()

            scores = scores_for_metadata(
                lm_rt,
                position_advancing=advancing,
                tmdb_matches=tmdb_ok,
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
            for key in ("identity", "position", "art", "app", "trt"):
                val = scores.get(key)
                if val is None:
                    continue
                _ln(f"confidence.{key}={float(val):.2f}")
            if scores.get("hdmi_charge"):
                _ln("hdmi.charge=true")
        except Exception:
            pass
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
    if isinstance(lm_rt, dict):
        _pp = str(lm_rt.get("prefer_pyatv_media") or "").strip().lower()
        if _pp in ("auto", "tv", "movie"):
            _ln(f"metadata.prefer_pyatv_media={_pp!r}")
        _ip = str(lm_rt.get("inferred_prefer") or "").strip().lower()
        if _ip in ("auto", "tv", "movie"):
            _ln(f"metadata.prefer_tmdb={_ip!r}")
    _ti = apple_tv_auto_state.get("last_tmdb_fetch_input")
    _tr = apple_tv_auto_state.get("last_tmdb_fetch_refined")
    _tp = apple_tv_auto_state.get("last_tmdb_fetch_prefer")
    if _ti is not None and str(_ti).strip():
        _ln(f"tmdbFetch.input_query={str(_ti)!r}")
    if _tr is not None and str(_tr).strip():
        _ln(f"tmdbFetch.refined_query={str(_tr)!r}")
    if _tp is not None and str(_tp).strip():
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


def _collect_view_four_source_lines(*, _view_four_display_metadata, _view_four_has_value, _view_four_text_is_placeholder, receiver_overlay_state) -> list[tuple[str, bool]]:
    """View 4 subview: best-effort file/stream stats from poll metadata + receiver text hints."""
    from math import gcd

    rows: list[tuple[str, bool]] = []

    def _ln(s: str, bold: bool = False) -> None:
        if _view_four_text_is_placeholder(s):
            return
        rows.append((s, bold))

    def _md_pick(md: dict[str, object], *keys: str) -> str | None:
        for k in keys:
            if k not in md:
                continue
            v = md[k]
            if v is None:
                continue
            if isinstance(v, (int, float)):
                if isinstance(v, float) and v != v:
                    continue
                t = str(int(v)) if float(v) == int(v) else str(v)
            else:
                t = str(v).strip()
            if t:
                return t
        return None

    def _aspect_from_wh(w_s: str | None, h_s: str | None) -> str | None:
        if not w_s or not h_s:
            return None
        try:
            wi = int(round(float(w_s)))
            hi = int(round(float(h_s)))
        except (TypeError, ValueError):
            return None
        if wi <= 0 or hi <= 0:
            return None
        g = gcd(wi, hi)
        return f"{wi // g}:{hi // g}"

    md = _view_four_display_metadata()
    inc = str(receiver_overlay_state.get("incoming") or "").strip()
    cfg = str(receiver_overlay_state.get("config") or "").strip()
    rx_blob = f"{inc} {cfg}".strip()

    if not isinstance(md, dict):
        _ln("(no last_metadata dict)", False)
        return rows

    w = _md_pick(md, "video_width", "width", "source_width", "ImageWidth", "image_width")
    h = _md_pick(md, "video_height", "height", "source_height", "ImageHeight", "image_height")
    res_one = _md_pick(
        md,
        "video_resolution",
        "source_resolution",
        "resolution",
        "VideoResolution",
    )

    def _ln_val(label: str, value: str | None) -> None:
        if _view_four_has_value(value):
            _ln(f"{label}: {value}", False)

    if res_one:
        _ln_val("Video source resolution", res_one)
    elif w and h:
        _ln_val("Video source resolution", f"{w}×{h}")
    elif w or h:
        _ln_val("Video source resolution", f"{w or '?'}×{h or '?'}")

    ar = _md_pick(md, "aspect_ratio", "video_aspect_ratio", "AspectRatio", "DisplayAspectRatio")
    if not ar:
        ar = _aspect_from_wh(w, h)
    _ln_val("Video source aspect ratio", ar)
    _ln_val(
        "Video source color space",
        _md_pick(
            md,
            "color_space",
            "color_primaries",
            "VideoColorSpace",
            "ColorSpace",
            "colour_space",
        ),
    )
    _ln_val(
        "Video source frame rate",
        _md_pick(
            md,
            "frame_rate",
            "framerate",
            "fps",
            "video_frame_rate",
            "FrameRate",
        ),
    )
    _ln_val(
        "Video source bit depth",
        _md_pick(md, "video_bit_depth", "bit_depth", "bits_per_pixel", "VideoBitDepth"),
    )
    _ln_val(
        "Video source codec",
        _md_pick(md, "video_codec", "codec", "video_format", "VideoCodec", "format"),
    )
    _ln_val(
        "Video source wrapper",
        _md_pick(md, "container", "wrapper", "mime_type", "MimeType", "file_extension"),
    )
    _ln_val(
        "Audio source wrapper",
        _md_pick(md, "audio_container", "audio_wrapper", "AudioContainer"),
    )
    _ln_val(
        "Audio source bit depth",
        _md_pick(md, "audio_bit_depth", "source_audio_bit_depth", "AudioBitDepth"),
    )
    _ln_val(
        "Audio source bit rate",
        _md_pick(md, "audio_bit_rate", "source_audio_bit_rate", "AudioBitrate", "audio_bitrate"),
    )
    _ln_val(
        "Audio source codec",
        _md_pick(md, "audio_codec", "audio_format", "AudioCodec", "AudioFormat"),
    )

    def _lpcm_vs_bitstream(blob: str, md2: dict[str, object]) -> str:
        ac = str(md2.get("audio_codec") or md2.get("audio_format") or "").lower()
        blob_l = blob.lower()
        joined = f"{ac} {blob_l}"
        if "pcm" in joined or "lpcm" in joined or "linear pcm" in joined:
            return "LPCM/PCM (from metadata/receiver text)"
        if any(
            x in joined
            for x in (
                "dolby",
                "dts",
                "truehd",
                "true-hd",
                "eac3",
                "e-ac-3",
                "atmos",
                "bitstream",
                "dd+",
                "dtsx",
            )
        ):
            return "Compressed / bitstream (from metadata/receiver text)"
        if blob:
            return "Unknown (see receiver lines below)"
        return "—"

    _lpcm = _lpcm_vs_bitstream(rx_blob, md)
    if _view_four_has_value(_lpcm):
        _ln(f"Audio source LPCM vs bitstream: {_lpcm}", False)

    proto = _md_pick(md, "protocol")
    if proto:
        _ln(f"Poll protocol: {proto}", False)
    appn = str(md.get("app_name") or "").strip()
    appid = str(md.get("app_id") or "").strip()
    if appn or appid:
        _ln(f"App: {appn!r} id={appid!r}", False)

    if inc:
        _ln(f"Receiver incoming (raw): {inc}", False)
    if cfg:
        _ln(f"Receiver config (raw): {cfg}", False)

    known = {
        "query",
        "title",
        "artist",
        "series_name",
        "album",
        "media_type",
        "total_time",
        "position",
        "device_state",
        "inferred_prefer",
        "prefer_pyatv_media",
        "content_key",
        "app_name",
        "app_id",
        "volume_percent",
        "prefer",
    }
    extra_keys = [
        k
        for k in sorted(md.keys())
        if k not in known
        and not str(k).startswith("_")
        and _view_four_has_value(md.get(k))
    ]
    if extra_keys:
        _ln("other metadata keys", False)
        for k in extra_keys[:36]:
            try:
                vv = md[k]
                rep = repr(vv)
                if len(rep) > 140:
                    rep = rep[:137] + "..."
            except Exception:
                rep = "?"
            _ln(f"  {k}={rep}", False)
        if len(extra_keys) > 36:
            _ln(f"  … ({len(extra_keys) - 36} more keys)", False)
    return rows
