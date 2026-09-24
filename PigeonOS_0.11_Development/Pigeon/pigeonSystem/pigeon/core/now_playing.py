"""Now-playing metadata queries: app detection, music/video artwork caches, playback clock maths.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

import time
import cv2
import numpy as np


def _metadata_is_netflix_app(metadata: dict[str, object] | None) -> bool:
    if not isinstance(metadata, dict):
        return False
    bid = str(metadata.get("app_id") or "").strip().lower()
    bid2 = str(
        metadata.get("bundle_identifier")
        or metadata.get("app_identifier")
        or metadata.get("bundle_id")
        or ""
    ).strip().lower()
    an = str(metadata.get("app_name") or "").strip().lower()
    return "netflix" in an or "netflix" in bid or "netflix" in bid2


def _playback_is_netflix_stream(*, _metadata_is_netflix_app, apple_tv_auto_state, streaming_badge_state) -> bool:
    lm = apple_tv_auto_state.get("last_metadata")
    if _metadata_is_netflix_app(lm if isinstance(lm, dict) else None):
        return True
    sb = streaming_badge_state
    lbl = str(sb.get("label") or "").strip().lower()
    fn = str(sb.get("filename") or "").strip().lower()
    return "netflix" in lbl or "netflix" in fn


def _clear_music_artwork_cache(*, apple_tv_auto_state) -> None:
    """Drop cached pyatv music artwork (leaving music / idle / track miss)."""
    if (
        apple_tv_auto_state.get("music_artwork_bgra") is None
        and apple_tv_auto_state.get("music_artwork_key") is None
    ):
        return
    apple_tv_auto_state["music_artwork_bgra"] = None
    apple_tv_auto_state["music_artwork_key"] = None


def _clear_video_artwork_cache(*, apple_tv_auto_state) -> None:
    """Drop cached pyatv YouTube / 16×9 artwork."""
    if (
        apple_tv_auto_state.get("video_artwork_bgra") is None
        and apple_tv_auto_state.get("video_artwork_key") is None
        and apple_tv_auto_state.get("youtube_thumb_key") is None
    ):
        return
    apple_tv_auto_state["video_artwork_bgra"] = None
    apple_tv_auto_state["video_artwork_key"] = None
    apple_tv_auto_state["youtube_thumb_key"] = None


def _clear_playback_artwork_caches(*, _clear_music_artwork_cache, _clear_video_artwork_cache) -> None:
    _clear_music_artwork_cache()
    _clear_video_artwork_cache()


def _music_artwork_track_key(md: dict[str, object]) -> str:
    return "|".join(
        (
            str(md.get("hash") or "").strip(),
            str(md.get("artwork_id") or "").strip(),
            str(md.get("title") or "").strip(),
            str(md.get("artist") or "").strip(),
            str(md.get("album") or "").strip(),
        )
    )


def _decode_artwork_bytes_bgra(raw: object) -> np.ndarray | None:
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        return None
    try:
        data = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    except Exception:
        return None
    if img is None or img.size == 0:
        return None
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    if img.shape[2] == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    if img.shape[2] >= 4:
        return img[:, :, :4].copy()
    return None


def _vv_has_content_title(*, _playback_display_title) -> bool:
    return bool(_playback_display_title())


def _vv_has_current_app(*, apple_tv_auto_state, streaming_badge_state) -> bool:
    if str(streaming_badge_state.get("filename") or "").strip():
        return True
    lm = apple_tv_auto_state.get("last_metadata")
    if isinstance(lm, dict):
        if str(lm.get("app_name") or "").strip():
            return True
        if str(lm.get("app_id") or "").strip():
            return True
    return False


def _vv_has_app_logo(*, _resolve_streaming_app_logo_bgra) -> bool:
    return _resolve_streaming_app_logo_bgra() is not None


def _vv_is_music(*, apple_tv_auto_state) -> bool:
    """True when the currently playing media is of type Music.

    Reads ``last_metadata['media_type']`` (populated via pyatv) and accepts
    both the pyatv ``MediaType.Music`` stringified form and the bare ``Music``
    label to be robust across metadata sources.
    """
    lm = apple_tv_auto_state.get("last_metadata")
    if not isinstance(lm, dict):
        return False
    mt = str(lm.get("media_type") or "").strip().lower()
    if not mt:
        return False
    return mt == "music" or mt.endswith(".music")


def _vv_is_youtube(*, apple_tv_auto_state, streaming_badge_state) -> bool:
    """True when the foreground streaming app is YouTube."""
    try:
        from pigeon.streaming_service_badges import is_youtube_streaming_service
    except Exception:
        is_youtube_streaming_service = None  # type: ignore[assignment]
    sb = streaming_badge_state
    label = str(sb.get("label") or "").strip()
    filename = str(sb.get("filename") or "").strip()
    app_name = ""
    app_id = ""
    lm = apple_tv_auto_state.get("last_metadata")
    if isinstance(lm, dict):
        app_name = str(lm.get("app_name") or "")
        app_id = str(lm.get("app_id") or "")
    if is_youtube_streaming_service is not None:
        if is_youtube_streaming_service(
            app_name=app_name,
            app_id=app_id,
            label=label,
            filename=filename,
        ):
            return True
    else:
        blob = f"{label} {filename} {app_name} {app_id}".lower()
        if "youtube" in blob:
            return True
    if isinstance(lm, dict):
        try:
            from pigeon.apple_tv_now_playing import youtube_video_id_from_metadata

            if youtube_video_id_from_metadata(lm):
                return True
        except Exception:
            pass
    return False


def _vv_music_track_title(*, apple_tv_auto_state) -> str:
    """Return the preferred Music track title for text rendering.

    Prefers ``title``; falls back to ``album`` (often the only populated
    field for certain streaming sources). Returns an empty string when
    nothing usable is available.
    """
    lm = apple_tv_auto_state.get("last_metadata")
    if not isinstance(lm, dict):
        return ""
    for k in ("title", "album"):
        v = str(lm.get(k) or "").strip()
        if v:
            return v
    return ""


def _vv_music_text_lines(*, apple_tv_auto_state) -> tuple[str, str]:
    """Return ``(title, subtitle)`` for Music text rendering.

    ``title`` is the track title (large top line); ``subtitle`` is the
    composed ``"Artist - Album"`` string (smaller line beneath) with a
    graceful collapse when either field is missing:

    * both present  → ``"Artist - Album"``
    * artist only   → ``"Artist"``
    * album only    → ``"Album"``
    * neither       → ``""``

    If ``title`` is empty but ``album`` is populated, ``album`` is
    promoted to ``title`` so the large line is never blank; the
    subtitle then collapses to just the artist (if any).
    """
    lm = apple_tv_auto_state.get("last_metadata")
    if not isinstance(lm, dict):
        return ("", "")
    title = str(lm.get("title") or "").strip()
    artist = str(lm.get("artist") or "").strip()
    album = str(lm.get("album") or "").strip()
    if not title and album:
        title, album = album, ""
    if artist and album:
        subtitle = f"{artist} - {album}"
    elif artist:
        subtitle = artist
    elif album:
        subtitle = album
    else:
        subtitle = ""
    return (title, subtitle)


def _stable_bgr_from_bgra(bgra: np.ndarray | None, *, _bgr_from_bgra_cache) -> np.ndarray | None:
    """BGR for a BGRA still. Same source array keeps the same destination."""
    if not isinstance(bgra, np.ndarray) or bgra.size == 0 or bgra.ndim != 3:
        _bgr_from_bgra_cache["src_id"] = None
        _bgr_from_bgra_cache["bgr"] = None
        return None
    sid = id(bgra)
    cached = _bgr_from_bgra_cache["bgr"]
    if sid == _bgr_from_bgra_cache["src_id"] and isinstance(cached, np.ndarray):
        return cached
    if bgra.shape[2] >= 4:
        out = np.ascontiguousarray(bgra[:, :, :3])
    elif bgra.shape[2] == 3:
        out = np.ascontiguousarray(bgra)
    else:
        _bgr_from_bgra_cache["src_id"] = None
        _bgr_from_bgra_cache["bgr"] = None
        return None
    _bgr_from_bgra_cache["src_id"] = sid
    _bgr_from_bgra_cache["bgr"] = out
    return out


def _paused_screen_artwork_bgr(*, _stable_bgr_from_bgra, apple_tv_auto_state) -> np.ndarray | None:
    """Album art as BGR, or None."""
    art = apple_tv_auto_state.get("music_artwork_bgra")
    return _stable_bgr_from_bgra(art if isinstance(art, np.ndarray) else None)


def _current_app_display_name(*, apple_tv_auto_state, streaming_badge_state) -> str:
    """Human-readable name for the currently foregrounded streaming app."""
    label = str(streaming_badge_state.get("label") or "").strip()
    if label:
        return label
    lm = apple_tv_auto_state.get("last_metadata")
    if isinstance(lm, dict):
        for k in ("app_name", "app_id"):
            v = str(lm.get(k) or "").strip()
            if v:
                return v
    return ""


def _player_duration_for_tmdb(*, apple_tv_auto_state, apple_tv_playback_clock) -> float | None:
    try:
        from pigeon.display_confidence import player_duration_seconds
    except ImportError:
        return None
    md = apple_tv_auto_state.get("last_metadata")
    clk = apple_tv_playback_clock
    return player_duration_seconds(
        md if isinstance(md, dict) else None,
        fallbacks=(clk.get("latched_total"), clk.get("last_reported_total")),
    )


def _playback_extrapolated_pair(*, apple_tv_playback_clock) -> tuple[int, int] | None:
    clk = apple_tv_playback_clock
    if clk.get("live_mode"):
        return None
    if not clk.get("has_sync"):
        return None
    now_m = time.monotonic()
    sp = float(clk["sync_position"])
    sm = float(clk["sync_mono"])
    pos = sp + (now_m - sm) if clk.get("playing") else sp
    pos = max(0.0, pos)
    lt = clk.get("latched_total")
    if lt is None:
        lt = clk.get("last_reported_total")
    if lt is not None:
        try:
            tft = float(lt)
            pos = min(pos, tft)
        except (TypeError, ValueError):
            tft = None
        else:
            played = int(pos)
            remaining = max(0, int(tft) - played)
            return played, remaining
    return int(pos), 0


def _playback_progress_fraction_for_bar(*, apple_tv_playback_clock) -> float | None:
    """Integer-second played / total for progress bar (None if duration unknown)."""
    clk = apple_tv_playback_clock
    if clk.get("live_mode"):
        return None
    if not clk.get("has_sync"):
        return None
    lt = clk.get("latched_total")
    if lt is None:
        lt = clk.get("last_reported_total")
    if lt is None:
        return None
    try:
        total_f = float(lt)
    except (TypeError, ValueError):
        return None
    if total_f <= 0:
        return None
    now_m = time.monotonic()
    sp = float(clk["sync_position"])
    sm = float(clk["sync_mono"])
    pos = sp + (now_m - sm) if clk.get("playing") else sp
    pos = max(0.0, min(pos, total_f))
    played_i = int(pos)
    total_i = max(1, int(round(total_f)))
    return max(0.0, min(1.0, played_i / float(total_i)))


def _trt_substantive_from_clock(*, apple_tv_playback_clock) -> bool:
    """True when we have duration-based played/remaining timecodes (not live / unknown TRT)."""
    clk = apple_tv_playback_clock
    if clk.get("live_mode"):
        return False
    if not clk.get("has_sync"):
        return False
    lt = clk.get("latched_total")
    if lt is None:
        lt = clk.get("last_reported_total")
    if lt is not None:
        try:
            if float(lt) > 0.0:
                return True
        except (TypeError, ValueError):
            pass
    # Position-only streams: show elapsed TRT even when total_time never arrives.
    if clk.get("playing"):
        try:
            return float(clk.get("sync_position") or 0.0) >= 0.0
        except (TypeError, ValueError):
            return False
    return False


def _trt_substantive_for_status_bar(*, _trt_substantive_from_clock) -> bool:
    return _trt_substantive_from_clock()


def _atv_metadata_is_content_idle(metadata: dict[str, object], *, metadata_has_playback_title, resolve_metadata_tmdb_query) -> bool:
    try:
        from pigeon.apple_tv_now_playing import apple_tv_power_is_off

        if apple_tv_power_is_off(metadata):
            return True
    except Exception:
        pass
    try:
        from pigeon.display_confidence import content_should_stay_active
        from pigeon.hdmi_ocr import hdmi_capture_available
        from pigeon.source_toggles import source_enabled

        if content_should_stay_active(
            metadata,
            hdmi_on=bool(source_enabled("hdmi")),
            hdmi_present=hdmi_capture_available(),
        ):
            return False
    except Exception:
        pass
    try:
        from pigeon.display_confidence import metadata_is_playback_idle

        return bool(metadata_is_playback_idle(metadata))
    except Exception:
        pass
    ds = str(metadata.get("device_state") or "")
    if resolve_metadata_tmdb_query is not None and resolve_metadata_tmdb_query(metadata):
        return False
    if metadata_has_playback_title is not None and metadata_has_playback_title(metadata):
        return False
    q = str(metadata.get("query") or "").strip()
    if q:
        return False
    if "Idle" in ds or "Stopped" in ds:
        return True
    if "Playing" in ds:
        return False
    return not q


def _program_audio_present(*, program_audio_present) -> bool:
    if program_audio_present is None:
        return False
    try:
        return bool(program_audio_present())
    except Exception:
        return False


def _program_audio_session(*, _program_audio_present, program_audio_session_present) -> bool:
    """True through quiet scenes after program audio has been heard."""
    if program_audio_session_present is not None:
        try:
            return bool(program_audio_session_present())
        except Exception:
            pass
    return _program_audio_present()


def _np_widgets_content_active(*, incoming: str = "", config: str = "", _apple_tv_is_off, _atv_metadata_is_content_idle, _program_audio_session, _show_paused_row_overlay, _something_playing_now, apple_tv_auto_state, apple_tv_playback_clock, receiver_standby_holder) -> bool:
    """True when NP should show more than the clock (title / play / AVR / audio)."""
    if _program_audio_session():
        return True
    if _apple_tv_is_off():
        return False
    if _something_playing_now() or _show_paused_row_overlay():
        return True
    if bool(apple_tv_playback_clock.get("live_mode")):
        return True
    lm = apple_tv_auto_state.get("last_metadata")
    if isinstance(lm, dict) and not _atv_metadata_is_content_idle(lm):
        return True
    if apple_tv_auto_state.get("tmdb_fetch_in_flight") or apple_tv_auto_state.get(
        "pending_tmdb"
    ):
        q = ""
        if isinstance(lm, dict):
            q = str(lm.get("query") or lm.get("title") or lm.get("ocr_title") or "").strip()
        if not q:
            q = str(apple_tv_auto_state.get("query") or "").strip()
        if q:
            return True
    if not bool(receiver_standby_holder[0]):
        if str(incoming or "").strip() or str(config or "").strip():
            return True
    return False


def _store_music_artwork_from_metadata(md: dict[str, object] | None, *, _atv_metadata_is_content_idle, _clear_music_artwork_cache, _clear_playback_artwork_caches, _clear_video_artwork_cache, _decode_artwork_bytes_bgra, _music_artwork_track_key, _vv_is_youtube, apple_tv_auto_state) -> None:
    """Decode/store pyatv artwork for Music covers or YouTube 16×9 thumbs."""
    if not isinstance(md, dict):
        _clear_playback_artwork_caches()
        return
    if _atv_metadata_is_content_idle(md):
        # YouTube often reports Idle on MRP while HDMI/Companion still play.
        # Keep a thumb we already have instead of flashing an empty zone 6.
        if not _vv_is_youtube():
            _clear_playback_artwork_caches()
            return
        if not md.get("artwork_bytes"):
            return
    mt = str(md.get("media_type") or "").strip().lower()
    is_music = mt == "music" or mt.endswith(".music")
    is_youtube = False
    try:
        from pigeon.streaming_service_badges import is_youtube_streaming_service

        is_youtube = bool(
            is_youtube_streaming_service(
                app_name=str(md.get("app_name") or ""),
                app_id=str(md.get("app_id") or ""),
            )
        )
    except Exception:
        blob = f"{md.get('app_name') or ''} {md.get('app_id') or ''}".lower()
        is_youtube = "youtube" in blob
    if not is_youtube:
        try:
            is_youtube = bool(_vv_is_youtube())
        except Exception:
            pass
    track_key = _music_artwork_track_key(md)
    art_bytes = md.get("artwork_bytes")
    art_id = md.get("artwork_id")
    sig = (
        track_key,
        str(art_id or ""),
        int(len(art_bytes)) if isinstance(art_bytes, (bytes, bytearray)) else 0,
    )
    have = apple_tv_auto_state.get("music_artwork_bgra")
    if not isinstance(have, np.ndarray) or have.size == 0:
        have = apple_tv_auto_state.get("video_artwork_bgra")
    if (
        apple_tv_auto_state.get("decoded_artwork_sig") == sig
        and isinstance(have, np.ndarray)
        and have.size > 0
    ):
        return
    bgra = _decode_artwork_bytes_bgra(art_bytes)
    apple_tv_auto_state["decoded_artwork_sig"] = sig
    if is_music:
        _clear_video_artwork_cache()
        prev_key = apple_tv_auto_state.get("music_artwork_key")
        if bgra is not None:
            apple_tv_auto_state["music_artwork_bgra"] = bgra
            apple_tv_auto_state["music_artwork_key"] = track_key
            return
        if track_key != prev_key:
            apple_tv_auto_state["music_artwork_bgra"] = None
            apple_tv_auto_state["music_artwork_key"] = track_key
        return
    if is_youtube:
        _clear_music_artwork_cache()
        prev_key = apple_tv_auto_state.get("video_artwork_key")
        if bgra is not None:
            apple_tv_auto_state["video_artwork_bgra"] = bgra
            apple_tv_auto_state["video_artwork_key"] = track_key
            return
        if track_key != prev_key:
            apple_tv_auto_state["video_artwork_bgra"] = None
            apple_tv_auto_state["video_artwork_key"] = track_key
        return
    # Unknown-app landscape art from pyatv is treated as a 16×9 thumb.
    try:
        from pigeon.np_layout import poster_image_is_16x9

        landscape = bool(bgra is not None and poster_image_is_16x9(bgra))
    except Exception:
        landscape = False
    if landscape:
        _clear_music_artwork_cache()
        apple_tv_auto_state["video_artwork_bgra"] = bgra
        apple_tv_auto_state["video_artwork_key"] = track_key
        return
    _clear_playback_artwork_caches()


def _update_status_bar_from_metadata(metadata: dict[str, object] | None, *, _apply_playback_clock_from_poll, _sync_trt_text_to_true_once) -> None:
    if metadata:
        _apply_playback_clock_from_poll(metadata)
    # Sync TRT digits to the latest polled integer second. The steady 1 Hz metronome
    # continues stepping from this anchor.
    _sync_trt_text_to_true_once()
