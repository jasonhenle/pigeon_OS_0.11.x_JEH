"""Clock saver / pausesaver / idle-activity decisions.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

import time
import numpy as np
from pigeon.clock_saver_policy import pausesaver_hold_from_metadata_class
from pigeon.clock_saver_policy import player_reports_playing
from pigeon.clock_saver_policy import tick_pause_hold
from PIL import ImageFont
from pigeon.compositing import cv_resize_interp
import cv2
import os


def _bump_pigeon_user_activity(_event: object | None = None, *, _boot_clock_saver_until_playback, last_metadata_activity_mono, last_pigeon_user_activity_mono) -> None:
    """Marks local UI activity (theater idle-dim + dismiss metadata-idle clock saver)."""
    now_act = time.monotonic()
    last_pigeon_user_activity_mono[0] = now_act
    # Any Pigeon control (key, click, rotary, settings) exits the 2-minute saver.
    last_metadata_activity_mono[0] = now_act
    _boot_clock_saver_until_playback[0] = False


def _show_paused_row_overlay(*, _apple_tv_is_off, _atv_metadata_is_content_idle, apple_tv_auto_state, apple_tv_playback_clock) -> bool:
    """True when the player has substantive content loaded but is not actively playing."""
    if _apple_tv_is_off():
        return False
    lm_raw = apple_tv_auto_state.get("last_metadata")
    lm = lm_raw if isinstance(lm_raw, dict) else None
    if lm is None:
        return False
    if _atv_metadata_is_content_idle(lm):
        return False
    clk = apple_tv_playback_clock
    if clk.get("live_mode"):
        return False
    ds = str(lm.get("device_state") or "")
    if "Playing" in ds:
        return False
    if "Idle" in ds:
        try:
            from pigeon.display_confidence import playback_has_concluded

            return bool(playback_has_concluded(lm))
        except Exception:
            return False
    has_title = bool(
        str(lm.get("title") or "").strip()
        or str(lm.get("query") or "").strip()
        or str(lm.get("artist") or "").strip()
    )
    if clk.get("has_sync"):
        return not bool(clk.get("playing"))
    if not has_title:
        return False
    return "Paused" in ds or "Pause" in ds or "Stopped" in ds


def _vol_norm_for_clock_saver(vol_raw: object) -> str:
    if vol_raw is None:
        return ""
    try:
        v = float(vol_raw)
        if v != v:
            return ""
        return f"{v:.6g}"
    except (TypeError, ValueError):
        return str(vol_raw).strip()


def _bump_clock_saver_significant_device(*, last_clock_saver_significant_device_mono) -> None:
    last_clock_saver_significant_device_mono[0] = time.monotonic()


def _note_metadata_activity(now: float | None = None, *, last_metadata_activity_mono) -> None:
    """Refresh the 2-minute metadata-idle clock-saver stamp."""
    last_metadata_activity_mono[0] = float(
        time.monotonic() if now is None else now
    )


def _clear_reported_position_stall_stamp(*, last_metadata_activity_mono) -> None:
    """Reset metadata-idle saver stamp (device removed / location change)."""
    last_metadata_activity_mono[0] = time.monotonic()


def _coarse_device_state_for_saver(ds: str) -> str:
    d = str(ds or "").strip().lower()
    if "playing" in d:
        return "playing"
    if "paus" in d:
        return "paused"
    if (not d) or ("idle" in d) or ("stop" in d):
        return "idle"
    return d


def _reset_clock_saver_device_signal_baseline(*, _bump_clock_saver_significant_device, _cs_sig_ck, _cs_sig_ds, _cs_sig_fp, _cs_sig_init, _cs_sig_vol, _note_metadata_activity) -> None:
    _cs_sig_init[0] = False
    _cs_sig_ck[0] = None
    _cs_sig_ds[0] = ""
    _cs_sig_vol[0] = ""
    _cs_sig_fp[0] = ""
    _bump_clock_saver_significant_device()
    _note_metadata_activity()


def _something_playing_now(*, _apple_tv_is_off, _atv_metadata_is_content_idle, apple_tv_auto_state, apple_tv_playback_clock) -> bool:
    """True when the player reports active playback (not idle/home/paused-only).

    MRP often says Idle while HDMI is still playing a titled show. A
    held / displayable identity still counts as playing. A stale
    ``playback_clock.playing`` flag must not win over Paused / Stopped.
    """
    if _apple_tv_is_off():
        return False
    lm = apple_tv_auto_state.get("last_metadata")
    md = lm if isinstance(lm, dict) else {}
    if player_reports_playing(
        md.get("device_state"),
        clock_playing=bool(apple_tv_playback_clock.get("playing")),
    ):
        return True
    if not isinstance(lm, dict):
        return False
    ds = str(lm.get("device_state") or "")
    if "Paused" in ds or "Stopped" in ds:
        return False
    try:
        from pigeon.display_confidence import playback_has_concluded

        if playback_has_concluded(md):
            return False
    except Exception:
        pass
    return not _atv_metadata_is_content_idle(lm)


def _metadata_drives_clock_saver(*, apple_tv_auto_state, apple_tv_playback_clock) -> bool:
    """True when player metadata (not HDMI OCR streak) owns clock-saver idle.

    Position progress is the primary signal. When metadata is driving, the
    HDMI 24-OCR unchanged-frame rule is ignored.
    """
    try:
        from pigeon.source_toggles import source_enabled

        if not source_enabled("metadata"):
            return False
    except Exception:
        pass
    if bool(apple_tv_playback_clock.get("has_sync")):
        return True
    md = apple_tv_auto_state.get("last_metadata")
    if not isinstance(md, dict):
        return False
    try:
        from pigeon.display_confidence import player_metadata_adequate

        return bool(player_metadata_adequate(md))
    except Exception:
        return bool(str(md.get("query") or md.get("title") or "").strip())


def _clock_saver_idle_need() -> float:
    try:
        from pigeon.widgets.options_settings import clock_saver_idle_s

        return float(clock_saver_idle_s())
    except Exception:
        return 60.0


def _clock_saver_user_enabled() -> bool:
    try:
        from pigeon.widgets.options_settings import clock_saver_enabled

        return bool(clock_saver_enabled())
    except Exception:
        return True


def _pausesaver_is_holding(*, _player_metadata_class, _show_paused_row_overlay) -> bool:
    """True while paused/stopped content should age toward Clocksaver.

    A held title can still look like playback. Matching auto widgets'
    ``stopped`` class keeps the pause timer running instead of resetting.
    """
    try:
        return pausesaver_hold_from_metadata_class(_player_metadata_class())
    except Exception:
        return bool(_show_paused_row_overlay())


def _refresh_paused_row_stamp(now: float, *, _paused_row_last_hold_mono, _paused_row_since_mono, _pausesaver_is_holding) -> float:
    """Seconds the current title has stayed on Pausesaver; 0 if not holding."""
    paused = _pausesaver_is_holding()
    age, since, last = tick_pause_hold(
        paused,
        now,
        float(_paused_row_since_mono[0]),
        float(_paused_row_last_hold_mono[0]),
    )
    _paused_row_since_mono[0] = since
    _paused_row_last_hold_mono[0] = last
    return age


def _clock_saver_content_is_idle(*, _atv_metadata_is_content_idle, apple_tv_auto_state) -> bool:
    lm = apple_tv_auto_state.get("last_metadata")
    if not isinstance(lm, dict):
        return True
    return bool(_atv_metadata_is_content_idle(lm))


def _idle_saver_face_toggle_ok(now: float | None = None, *, _clock_saver_for_compose, _clock_startup_intro_opacity, startup_ph) -> bool:
    """Idle clock/meter face is up (not splash intro, not NP chrome)."""
    t = time.monotonic() if now is None else float(now)
    if _clock_startup_intro_opacity(t) is not None:
        return False
    if startup_ph[0] is not None:
        return False
    return bool(_clock_saver_for_compose(t))


def _audio_capture_wanted(now: float | None = None, *, _idle_audio_listen, _idle_audio_meter_active, _np_wants_live_audio, _settings_audio_led_listen) -> bool:
    if _idle_audio_meter_active(now):
        return True
    if _settings_audio_led_listen():
        return True
    if _np_wants_live_audio():
        return True
    return _idle_audio_listen(now)


def _clock_saver_dim_pre_digit_canvas(canvas: np.ndarray, dim: float) -> None:
    """Darken backdrop/gradient/mic before drawing saver glyphs so only the large time reads at full brilliance."""
    d = max(0.0, min(1.0, float(dim)))
    if d >= 1.0 - 1e-9:
        return
    canvas[:] = (canvas.astype(np.float32) * d).clip(0, 255).astype(np.uint8)


def _clock_saver_dim_overlay_bgra(patch_bgra: np.ndarray, dim: float) -> np.ndarray:
    """Idle-dim streaming badge / receiver rows during clock saver (alpha scale; callers may skip if dim≈1)."""
    d = max(0.0, min(1.0, float(dim)))
    if d >= 1.0 - 1e-9:
        return patch_bgra
    out = patch_bgra.copy().astype(np.float32)
    out[:, :, 3] *= d
    return np.clip(out, 0, 255).astype(np.uint8)


def _saver_layer_is_full_frame(
    canvas: np.ndarray,
    overlay_bgra: np.ndarray,
    rect: tuple[int, int, int, int],
) -> bool:
    sx, sy, sw, sh = (int(v) for v in rect)
    return (
        sx == 0
        and sy == 0
        and sw == int(canvas.shape[1])
        and sh == int(canvas.shape[0])
        and int(overlay_bgra.shape[0]) == int(canvas.shape[0])
        and int(overlay_bgra.shape[1]) == int(canvas.shape[1])
        and int(overlay_bgra.shape[2]) >= 3
    )


def _set_playback_overlay_clock_saver_volume_flag(*, _backdrop_active_for_view, _clock_saver_for_compose, _playback_is_netflix_stream, playback_overlay_flags, receiver_overlay_state) -> None:
    """True while clock saver is active and the receiver shows a real volume string."""
    from pigeon.widgets.playback_overlay import _receiver_volume_display_line

    vol = _receiver_volume_display_line(receiver_overlay_state.get("volume", ""))
    cs_ok = _clock_saver_for_compose(time.monotonic())
    nf_bd = bool(cs_ok and _backdrop_active_for_view() and _playback_is_netflix_stream())
    playback_overlay_flags["clock_saver_netflix_full_overlay"] = nf_bd
    playback_overlay_flags["clock_saver_volume_only"] = bool(
        cs_ok and vol and not nf_bd
    )


def _idle_audio_meter_active(now: float | None = None, *, _idle_saver_face_toggle_ok, audio_meter_face_enabled) -> bool:
    """True when the diagnostic SVG meter should replace the idle clock."""
    if audio_meter_face_enabled is None or not audio_meter_face_enabled():
        return False
    return _idle_saver_face_toggle_ok(now)


def _blit_saver_layers_design(
    canvas: np.ndarray,
    time_bgra: np.ndarray,
    t_rect: tuple[int, int, int, int],
    date_bgra: np.ndarray,
    d_rect: tuple[int, int, int, int],
    *,
    copy_full_bgr: bool = False,
    _saver_layer_is_full_frame, alpha_blend_bgra_over_bgr,
) -> None:
    """Blit saver layers. Full-frame meter art is opaque BGR — skip alpha."""
    for cs_bgra, rect in ((date_bgra, d_rect), (time_bgra, t_rect)):
        sx, sy, sw, sh = (int(v) for v in rect)
        if copy_full_bgr and _saver_layer_is_full_frame(canvas, cs_bgra, rect):
            np.copyto(canvas, cs_bgra[:, :, :3])
            continue
        roi2 = canvas[sy : sy + sh, sx : sx + sw]
        roi2[:] = alpha_blend_bgra_over_bgr(roi2, cs_bgra)


def _blit_saver_layers_target(
    base: np.ndarray,
    time_bgra: np.ndarray,
    t_rect: tuple[int, int, int, int],
    date_bgra: np.ndarray,
    d_rect: tuple[int, int, int, int],
    cap_w: int,
    cap_h: int,
    *,
    copy_full_bgr: bool = False,
    DESIGN_H, DESIGN_W, _design_rect_to_target, alpha_blend_bgra_over_bgr,
) -> None:
    for cs_bgra, rect in ((date_bgra, d_rect), (time_bgra, t_rect)):
        sx, sy, sw, sh = (int(v) for v in rect)
        x, y, rw, rh = _design_rect_to_target(sx, sy, sw, sh, cap_w, cap_h)
        _ch, _cw = cs_bgra.shape[:2]
        if (
            copy_full_bgr
            and sx == 0
            and sy == 0
            and sw == int(DESIGN_W)
            and sh == int(DESIGN_H)
            and int(x) == 0
            and int(y) == 0
            and int(rw) == int(base.shape[1])
            and int(rh) == int(base.shape[0])
        ):
            bgr = cs_bgra[:, :, :3]
            if _cw == rw and _ch == rh:
                np.copyto(base, bgr)
            else:
                np.copyto(
                    base,
                    cv2.resize(
                        bgr,
                        (rw, rh),
                        interpolation=cv_resize_interp(_cw, _ch, rw, rh),
                    ),
                )
            continue
        patch = cv2.resize(
            cs_bgra, (rw, rh), interpolation=cv_resize_interp(_cw, _ch, rw, rh)
        )
        sub = base[y : y + rh, x : x + rw]
        sub[:] = alpha_blend_bgra_over_bgr(sub, patch)


def _paused_screen_font(px: int, *, _paused_screen_font_cache) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    size = max(24, int(px))
    cached = _paused_screen_font_cache.get(size)
    if cached is not None:
        return cached
    paths = (
        os.environ.get("PIGEON_FONT_MEDIUM", ""),
        os.environ.get("PIGEON_FONT_EXTRABOLD", ""),
        os.environ.get("PIGEON_FONT", ""),
    )
    for fp in paths:
        if fp and os.path.isfile(fp):
            try:
                font = ImageFont.truetype(fp, size=size)
                _paused_screen_font_cache[size] = font
                return font
            except Exception:
                pass
    font = ImageFont.load_default()
    _paused_screen_font_cache[size] = font
    return font


def _compose_paused_screen(cap_w: int, cap_h: int, *, PAUSED_SCREEN_BACKDROP_DIM, _paused_screen_backdrop_bgr, _paused_screen_font) -> np.ndarray:
    from pigeon.paused_screen import compose_pausesaver_bgr

    src = _paused_screen_backdrop_bgr()
    font = _paused_screen_font(max(52, int(round(float(cap_h) * 0.13))))
    return compose_pausesaver_bgr(
        int(cap_w),
        int(cap_h),
        src,
        font=font,
        dim=PAUSED_SCREEN_BACKDROP_DIM,
    )


def _clock_saver_layer_opacity(now: float, *, CLOCK_SAVER_DIM_OPACITY, _boot_clock_saver_until_playback, _clock_startup_intro_opacity, _splash_reveal_clock, clock_saver_peek_until_mono) -> float:
    intro = _clock_startup_intro_opacity(now)
    if intro is not None:
        return float(intro)
    if now < clock_saver_peek_until_mono[0]:
        return 1.0
    # Boot / splash-reveal: full-on clock (no ease from black, no idle dim).
    if _boot_clock_saver_until_playback[0] or _splash_reveal_clock[0]:
        return 1.0
    return CLOCK_SAVER_DIM_OPACITY
