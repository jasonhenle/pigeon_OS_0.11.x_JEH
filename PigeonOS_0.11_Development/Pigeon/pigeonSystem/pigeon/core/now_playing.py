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
        from pigeon.hdmi_capture import hdmi_capture_available

        if content_should_stay_active(
            metadata,
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
            q = str(lm.get("query") or lm.get("title") or "").strip()
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


def _sync_now_playing_screen_state_for_frame(*, _np_state_sync_mono, _sync_now_playing_screen_state, view_circles_widget) -> None:
    """Throttle NP metadata while live audio widgets are painting at 30 Hz."""
    live = False
    try:
        live = bool(
            view_circles_widget is not None
            and view_circles_widget._live_audio_widgets_on()
        )
    except Exception:
        live = False
    if live:
        t_sync = time.monotonic()
        if t_sync - _np_state_sync_mono[0] < 1.0:
            return
        _np_state_sync_mono[0] = t_sync
    _sync_now_playing_screen_state()


def _clear_now_playing_view_caches(*, view_circles_widget) -> None:
    if view_circles_widget is not None:
        view_circles_widget.clear_cache()


def _playback_display_title(*, active_tmdb_display_title, apple_tv_auto_state) -> str:
    """Best on-screen title: TMDb display name, else Apple TV metadata."""
    if (active_tmdb_display_title[0] or "").strip():
        return str(active_tmdb_display_title[0]).strip()
    lm = apple_tv_auto_state.get("last_metadata")
    if isinstance(lm, dict):
        for key in ("title", "series_name", "query", "artist"):
            s = str(lm.get(key) or "").strip()
            if s:
                return s
    return ""


def _np_drawing_live_audio(*, _clock_saver_for_compose, _view_one_uses_now_playing_screen, view_circles_widget) -> bool:
    """True when NP is actually painting visualizer / VU / levels this frame.

    Capture can stay on for countdown widgets; skip-cache cadence follows
    what is on screen. Now-playing forces ``scene_enabled`` off, so this
    must also drive the scene-off skip key (otherwise the well is 1 Hz).
    """
    if view_circles_widget is None:
        return False
    if not _view_one_uses_now_playing_screen():
        return False
    try:
        if _clock_saver_for_compose(time.monotonic()):
            return False
    except Exception:
        pass
    try:
        return bool(view_circles_widget._live_audio_widgets_on())
    except Exception:
        return False


def _apply_playback_clock_from_poll(metadata: dict[str, object], *, _content_key_from_metadata, apple_tv_playback_clock, last_timecode_motion_mono) -> None:
    """Anchor wall clock to last reported position; polls resync and correct drift."""
    clk = apple_tv_playback_clock
    ds = str(metadata.get("device_state") or "")
    playing_now = "Playing" in ds
    now_m = time.monotonic()
    content_key = _content_key_from_metadata(metadata)
    prev_has_sync = bool(clk.get("has_sync"))
    prev_pos = float(clk.get("sync_position") or 0.0)

    tt_raw = metadata.get("total_time")
    try:
        reported_total = float(tt_raw) if tt_raw is not None else None
    except (TypeError, ValueError):
        reported_total = None
    if reported_total is not None:
        try:
            if float(reported_total) > 0:
                clk["last_reported_total"] = reported_total
        except (TypeError, ValueError):
            pass
    elif content_key and content_key == clk.get("latched_content_key"):
        # pyatv on some paths (often Linux) omits total_time while still reporting position.
        for key in ("latched_total", "last_reported_total"):
            prev = clk.get(key)
            if prev is None:
                continue
            try:
                pf = float(prev)
            except (TypeError, ValueError):
                continue
            if pf > 0:
                reported_total = pf
                break

    pos_raw = metadata.get("position")
    pos_f: float | None = None
    if pos_raw is not None:
        try:
            pos_f = max(0.0, float(pos_raw))
        except (TypeError, ValueError):
            pos_f = None

    # Live/continuous content: playing with no duration and no scrub position.
    live_now = bool(playing_now) and (reported_total is None or reported_total <= 0)
    if live_now and pos_f is not None:
        live_now = False
    if live_now:
        clk["live_mode"] = True
        clk["has_sync"] = False
        clk["latched_total"] = None
        clk["display_played_sec"] = None
        clk["trt_next_fire_mono"] = None
        clk["playing"] = True
        # Still latch content so TMDb artwork doesn't keep swapping.
        if content_key and content_key != clk.get("latched_content_key"):
            clk["latched_content_key"] = content_key
        # Live has no position; metadata fingerprint (title/state) alone
        # decides whether the 2-minute idle saver arms.
        return
    clk["live_mode"] = False

    if content_key and content_key != clk.get("latched_content_key"):
        clk["latched_content_key"] = content_key
        clk["latched_total"] = reported_total
        clk["display_played_sec"] = None
        clk["trt_next_fire_mono"] = None

    if pos_f is not None:
        clk["sync_mono"] = now_m
        clk["sync_position"] = pos_f
        clk["playing"] = playing_now
        clk["has_sync"] = True
        pos_moved = not prev_has_sync or abs(pos_f - prev_pos) >= 0.25
        if playing_now and pos_moved:
            last_timecode_motion_mono[0] = now_m
        # Metadata-idle saver stamp is refreshed via fingerprint (incl. position)
        # in ``_bump_clock_saver_significant_device_from_metadata``.
        return

    if not clk.get("has_sync"):
        clk["playing"] = playing_now
        return

    sp = float(clk["sync_position"])
    sm = float(clk["sync_mono"])
    extrap = sp + (now_m - sm) if clk.get("playing") else sp
    extrap = max(0.0, extrap)
    lt = clk.get("latched_total")
    if lt is not None:
        try:
            extrap = min(extrap, float(lt))
        except (TypeError, ValueError):
            pass
    clk["sync_position"] = extrap
    clk["sync_mono"] = now_m
    clk["playing"] = playing_now
    if playing_now and abs(extrap - sp) >= 0.25:
        last_timecode_motion_mono[0] = now_m


def _sync_status_bar_trt_substantive(*, _trt_substantive_for_status_bar, _warm_status_bar_blits, skip_cache, status_bar_widget) -> None:
    if status_bar_widget is None:
        return
    if status_bar_widget.set_trt_substantive(_trt_substantive_for_status_bar()):
        _warm_status_bar_blits()
        skip_cache[0] = None


def _sync_status_bar_visibility_for_playback(metadata: dict[str, object] | None, *, DisplayView, _atv_metadata_is_content_idle, _effective_display_view, _np_widgets_content_active, _resolve_receiver_lines_for_now_playing, _sync_now_playing_screen_state, _sync_status_bar_trt_substantive, _warm_status_bar_blits, apple_tv_playback_clock, current_apple_tv, skip_cache, status_bar_widget) -> None:
    """Hide now-playing bar + TRT pills when idle; show when content / AVR is live."""
    if status_bar_widget is None:
        return
    if _effective_display_view() == DisplayView.ONE:
        try:
            inc, cfg, _vol = _resolve_receiver_lines_for_now_playing()
        except Exception:
            inc, cfg = "", ""
        show = _np_widgets_content_active(incoming=inc, config=cfg)
    elif not current_apple_tv.get("identifier"):
        show = False
    elif metadata is not None:
        show = not _atv_metadata_is_content_idle(metadata)
    else:
        clk = apple_tv_playback_clock
        show = bool(clk.get("has_sync"))
    if status_bar_widget.set_now_playing_chrome_visible(show):
        _warm_status_bar_blits()
        skip_cache[0] = None
    _sync_status_bar_trt_substantive()
    _sync_now_playing_screen_state()


def _refresh_trt_progress_only(*, _playback_progress_fraction_for_bar, _sync_now_playing_screen_state, _warm_status_bar_blits, skip_cache, status_bar_widget) -> None:
    if status_bar_widget is None:
        return
    pfrac = _playback_progress_fraction_for_bar()
    prog = pfrac if pfrac is not None else 0.0
    if status_bar_widget.set_now_playing_display(progress=prog):
        _warm_status_bar_blits()
        skip_cache[0] = None
    _sync_now_playing_screen_state()


def _refresh_extrapolated_timecodes(*, tick_steps: int = 1, _format_hmmss, _idle_audio_meter_active, _playback_extrapolated_pair, _playback_progress_fraction_for_bar, _sync_status_bar_trt_substantive, _warm_status_bar_blits, apple_tv_playback_clock, skip_cache, status_bar_widget) -> None:
    """Update TRT labels + progress using stepped display time (steady rhythm)."""
    if status_bar_widget is None:
        return
    if _idle_audio_meter_active():
        return
    try:
        clk = apple_tv_playback_clock
        if clk.get("live_mode"):
            if status_bar_widget.set_now_playing_display(
                played_text="LIVE",
                remaining_text="",
                progress=0.0,
            ):
                _warm_status_bar_blits()
                skip_cache[0] = None
            return
        pair = _playback_extrapolated_pair()
        pfrac = _playback_progress_fraction_for_bar()
        if pair is None:
            clk["display_played_sec"] = None
            clk["trt_next_fire_mono"] = None
            if status_bar_widget.set_now_playing_display(progress=0.0):
                _warm_status_bar_blits()
                skip_cache[0] = None
            return
        played_true, _rem_true = pair
        lt_use = clk.get("latched_total")
        if lt_use is None:
            lt_use = clk.get("last_reported_total")
        playing = bool(clk.get("playing"))
        disp = clk.get("display_played_sec")

        if disp is None:
            disp = int(played_true)
        elif not playing:
            disp = int(played_true)
        else:
            pt = int(played_true)
            diff = pt - disp
            # Keep the on-screen cadence steady: at most ±1 per tick for small drift.
            # Snap only for major seeks (e.g. user scrubs).
            if diff >= 10:
                disp = pt
            elif diff > 0:
                disp = min(pt, disp + 1)
            elif diff <= -10:
                disp = pt
            elif diff < 0:
                disp = max(pt, disp - 1)
        clk["display_played_sec"] = disp

        if lt_use is not None:
            try:
                tft = int(round(float(lt_use)))
                remaining_secs = max(0, tft - disp)
            except (TypeError, ValueError):
                remaining_secs = max(0, int(_rem_true) + int(played_true) - disp)
        else:
            remaining_secs = max(0, int(_rem_true) + int(played_true) - disp)

        played_text = _format_hmmss(disp)
        remaining_text = _format_hmmss(remaining_secs)
        prog = pfrac if pfrac is not None else 0.0
        if status_bar_widget.set_now_playing_display(
            played_text=played_text,
            remaining_text=remaining_text,
            progress=prog,
        ):
            _warm_status_bar_blits()
            skip_cache[0] = None
    finally:
        _sync_status_bar_trt_substantive()


def _warm_status_bar_blits(*, status_bar_blits, status_bar_widget) -> None:
    if status_bar_widget is None:
        status_bar_blits[0] = []
        return
    status_bar_blits[0] = list(status_bar_widget.design_blits())
