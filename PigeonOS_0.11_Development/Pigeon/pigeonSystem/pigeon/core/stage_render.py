"""Stage / frame rendering helpers and render scheduling.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import tkinter as tk
import cv2
import numpy as np
from pigeon.stage_background import bgr_to_tk_hex
from pigeon.stage_background import get_stage_bgr
from pigeon.compositing import cv_resize_interp
import sys
from pigeon.version import version_string
from pathlib import Path
from pigeon.stage_background import set_stage_bgr
import time

if TYPE_CHECKING:
    from pigeon_0_9 import DisplayView, SceneFit


def _black_screen_bgr(*, display_dims) -> np.ndarray:
    sb, sg, sr = get_stage_bgr()
    out = np.empty((display_dims[1], display_dims[0], 3), dtype=np.uint8)
    out[:] = (sb, sg, sr)
    return out


# TopGradient.png removed — top gradient overlay disabled (no-op).
def _blend_top_gradient_design(canvas: np.ndarray) -> None:
    return


def _blend_top_gradient_fast(base: np.ndarray, cap_w: int, cap_h: int) -> None:
    return


def _apply_stage_chrome_colors(*, label, video_area) -> None:
    b, g, r = get_stage_bgr()
    hx = bgr_to_tk_hex(b, g, r)
    try:
        video_area.configure(bg=hx)
        label.configure(bg=hx)
    except tk.TclError:
        pass


def _design_rect_to_window(wx: int, wy: int, ww: int, wh: int, *, _design_rect_to_target, display_dims) -> tuple[int, int, int, int]:
    return _design_rect_to_target(wx, wy, ww, wh, display_dims[0], display_dims[1])


def _styled_video_content_c_poster(src_bgra: np.ndarray | None) -> np.ndarray | None:
    """Return poster BGRA with rounded corners + faint white border for viewOne.videoContent_c."""
    if src_bgra is None or src_bgra.size == 0 or src_bgra.ndim != 3:
        return None
    if src_bgra.shape[2] == 4:
        out = src_bgra.copy()
    elif src_bgra.shape[2] == 3:
        out = cv2.cvtColor(src_bgra, cv2.COLOR_BGR2BGRA)
    else:
        return None
    h, w = int(out.shape[0]), int(out.shape[1])
    if h < 4 or w < 4:
        return out

    radius = max(6, int(round(min(w, h) * 0.035)))
    radius = min(radius, max(1, (min(w, h) // 2) - 1))

    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.rectangle(mask, (radius, 0), (w - radius, h), 255, thickness=-1)
    cv2.rectangle(mask, (0, radius), (w, h - radius), 255, thickness=-1)
    cv2.circle(mask, (radius, radius), radius, 255, thickness=-1)
    cv2.circle(mask, (w - radius, radius), radius, 255, thickness=-1)
    cv2.circle(mask, (radius, h - radius), radius, 255, thickness=-1)
    cv2.circle(mask, (w - radius, h - radius), radius, 255, thickness=-1)

    out_alpha = out[:, :, 3].astype(np.float32) * (mask.astype(np.float32) / 255.0)
    out[:, :, 3] = np.clip(out_alpha, 0, 255).astype(np.uint8)

    stroke = max(1, int(round(min(w, h) * 0.006)))
    if stroke > 0 and (h - 2 * stroke) > 2 and (w - 2 * stroke) > 2:
        inner = np.zeros((h, w), dtype=np.uint8)
        ir = max(1, radius - stroke)
        cv2.rectangle(inner, (stroke + ir, stroke), (w - stroke - ir, h - stroke), 255, thickness=-1)
        cv2.rectangle(inner, (stroke, stroke + ir), (w - stroke, h - stroke - ir), 255, thickness=-1)
        cv2.circle(inner, (stroke + ir, stroke + ir), ir, 255, thickness=-1)
        cv2.circle(inner, (w - stroke - ir, stroke + ir), ir, 255, thickness=-1)
        cv2.circle(inner, (stroke + ir, h - stroke - ir), ir, 255, thickness=-1)
        cv2.circle(inner, (w - stroke - ir, h - stroke - ir), ir, 255, thickness=-1)
        border = cv2.subtract(mask, inner)
    else:
        border = np.zeros((h, w), dtype=np.uint8)
    if np.any(border > 0):
        out[border > 0, 0] = 255
        out[border > 0, 1] = 255
        out[border > 0, 2] = 255
        out[border > 0, 3] = np.maximum(out[border > 0, 3], 96)
    return out


def place_command_bar(*, _ui_scale, command_bar, display_dims) -> None:
    dw, dh = display_dims[0], display_dims[1]
    ui = _ui_scale()
    bar_h = max(24, int(32 * ui))
    yb = dh - bar_h - int(4 * ui)
    command_bar.place(x=0, y=yb, width=dw, height=bar_h)


def _apply_dev_phase_widgets(*, label, settings_frame) -> None:
    # settings_main composites onto the video label; never pack the Tk form.
    try:
        settings_frame.pack_forget()
    except tk.TclError:
        pass
    label.pack(fill=tk.BOTH, expand=True)


def _invoke_render_after(*, _render_after_id, render_once) -> None:
    _render_after_id[0] = None
    render_once()


def _schedule_render_oneshot(delay_ms: int, *, _invoke_render_after, _render_after_id, root) -> None:
    if _render_after_id[0] is not None:
        try:
            root.after_cancel(_render_after_id[0])
        except tk.TclError:
            pass
    _render_after_id[0] = root.after(max(1, int(delay_ms)), _invoke_render_after)


def _disp_fit(*, fit_holder) -> SceneFit:
    return fit_holder[0]


def _stage_grid_overlay_mode(*, DisplayView, display_view_holder, view_five_mode_holder) -> str:
    """Overlay mode for grid rendering."""
    if display_view_holder[0] == DisplayView.FIVE:
        vf = int(view_five_mode_holder[0])
        if vf == 1:
            return "absolute_lines_fractional"
        if vf == 2:
            return "absolute_lines_test"
        return "absolute_lines"
    return "legacy_grid"


def _record_live_audio_timing(t0: float, t1: float, t2: float, *, _hitch_parts, _live_perf) -> None:
    compose_ms = (t1 - t0) * 1000.0
    upload_ms = (t2 - t1) * 1000.0
    _live_perf[1] += 1.0
    _live_perf[2] += compose_ms
    _live_perf[3] += upload_ms
    if compose_ms > _live_perf[4]:
        _live_perf[4] = compose_ms
    if compose_ms >= 40.0:
        try:
            sys.stderr.write(
                "pigeon: live hitch "
                f"compose={compose_ms:.1f}ms "
                f"sync={_hitch_parts[0]:.1f}ms "
                f"render={_hitch_parts[1]:.1f}ms "
                f"upload={upload_ms:.1f}ms\n"
            )
        except Exception:
            pass
    if t2 - _live_perf[0] < 2.0 or _live_perf[1] < 1.0:
        return
    n = max(1.0, _live_perf[1])
    try:
        sys.stderr.write(
            "pigeon: live audio "
            f"{int(n)}f compose={_live_perf[2] / n:.1f}ms "
            f"max={_live_perf[4]:.1f}ms "
            f"upload={_live_perf[3] / n:.1f}ms\n"
        )
    except Exception:
        pass
    _live_perf[0] = t2
    _live_perf[1] = 0.0
    _live_perf[2] = 0.0
    _live_perf[3] = 0.0
    _live_perf[4] = 0.0


def _design_rect_to_target(
    wx: int, wy: int, ww: int, wh: int, Wt: int, Ht: int
, *, DESIGN_H, DESIGN_W) -> tuple[int, int, int, int]:
    """Map a design-canvas rectangle to target size Wt×Ht (same math as final window mapping)."""
    Wd, Hd = DESIGN_W, DESIGN_H
    scaled_w = int(round(Wd * Ht / float(Hd)))
    x_off = max(0, (scaled_w - Wt) // 2)

    def mx(xd: float) -> int:
        return int(round(xd * scaled_w / float(Wd))) - x_off

    def my(yd: float) -> int:
        return int(round(yd * Ht / float(Hd)))

    x0, y0 = mx(wx), my(wy)
    x1, y1 = mx(wx + ww), my(wy + wh)
    rw = max(1, x1 - x0)
    rh = max(1, y1 - y0)
    if x0 < 0:
        rw += x0
        x0 = 0
    if y0 < 0:
        rh += y0
        y0 = 0
    rw = min(rw, Wt - x0)
    rh = min(rh, Ht - y0)
    if rw < 1 or rh < 1:
        return (0, 0, 1, 1)
    return (x0, y0, rw, rh)


def _paste_bgra_contain_on_design(
    canvas_bgr: np.ndarray,
    patch_bgra: np.ndarray | None,
    rect: tuple[int, int, int, int] | list[int],
    *, DESIGN_H, DESIGN_W, alpha_blend_bgra_over_bgr,
) -> None:
    """Paste ``patch_bgra`` centered inside ``rect`` on a design-sized
    BGR canvas using uniform contain-fit (no crop)."""
    if patch_bgra is None or alpha_blend_bgra_over_bgr is None:
        return
    rx, ry, rw, rh = (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    if rw < 1 or rh < 1:
        return
    ph, pw = int(patch_bgra.shape[0]), int(patch_bgra.shape[1])
    if pw < 1 or ph < 1:
        return
    scale = min(rw / float(pw), rh / float(ph))
    nw = max(1, int(round(pw * scale)))
    nh = max(1, int(round(ph * scale)))
    rsz = cv2.resize(
        patch_bgra,
        (nw, nh),
        interpolation=cv_resize_interp(pw, ph, nw, nh),
    )
    ox = rx + (rw - nw) // 2
    oy = ry + (rh - nh) // 2
    dst_x0 = max(0, ox)
    dst_y0 = max(0, oy)
    dst_x1 = min(DESIGN_W, ox + nw)
    dst_y1 = min(DESIGN_H, oy + nh)
    if dst_x1 <= dst_x0 or dst_y1 <= dst_y0:
        return
    src_x0 = dst_x0 - ox
    src_y0 = dst_y0 - oy
    cw = dst_x1 - dst_x0
    ch = dst_y1 - dst_y0
    crop = rsz[src_y0 : src_y0 + ch, src_x0 : src_x0 + cw]
    sub = canvas_bgr[dst_y0:dst_y1, dst_x0:dst_x1]
    sub[:] = alpha_blend_bgra_over_bgr(sub, crop)


def _ui_scale(*, WINDOW_H, WINDOW_W, display_dims) -> float:
    dw, dh = display_dims[0], display_dims[1]
    return max(0.45, min(min(dw / float(WINDOW_W), dh / float(WINDOW_H)), 5.0))


def _location_toast_alpha(now: float, *, LOCATION_TOAST_FADE_S, LOCATION_TOAST_FULL_S, location_toast_state) -> float:
    st = location_toast_state
    if not st["active"]:
        return 0.0
    hold = float(st.get("hold_full_s", LOCATION_TOAST_FULL_S))
    elapsed = now - float(st["t0"])
    if elapsed < hold:
        return 1.0
    if elapsed < hold + LOCATION_TOAST_FADE_S:
        return max(0.0, 1.0 - (elapsed - hold) / LOCATION_TOAST_FADE_S)
    st["active"] = False
    return 0.0


def _info_cluster_compose_active(now_mono: float, *, DevPhase, DisplayView, _PIGEON_EXT, _clock_saver_for_compose, _effective_display_view, _view_one_uses_now_playing_screen, dev_phase) -> bool:
    if not _PIGEON_EXT:
        return False
    if dev_phase[0] != DevPhase.OFF:
        return False
    if _effective_display_view() == DisplayView.FOUR:
        return False
    if _clock_saver_for_compose(now_mono):
        return False
    # New now-playing screen (070326) draws clock, audio config, and volume.
    if _view_one_uses_now_playing_screen():
        return False
    return True


def _effective_display_view(*, DevPhase, DisplayView, _PIGEON_EXT, dev_phase, display_view_holder) -> DisplayView:
    """Logical display for composition. GRID / view 5 overlay preview as view 1 + snapshot layout."""
    if _PIGEON_EXT and (
        dev_phase[0] == DevPhase.GRID or display_view_holder[0] == DisplayView.FIVE
    ):
        return DisplayView.ONE
    return display_view_holder[0]


def _design_grid_overlay_active(*, DevPhase, DisplayView, dev_phase, display_view_holder) -> bool:
    """Grid overlay on the composite (developer GRID phase or view 5)."""
    return dev_phase[0] == DevPhase.GRID or display_view_holder[0] == DisplayView.FIVE


def _blend_info_cluster_into_target(
    target: np.ndarray, cap_w: int, cap_h: int, now_mono: float
, *, _design_rect_to_target, _info_cluster_compose_active, _warm_info_cluster_blits, alpha_blend_bgra_over_bgr, info_cluster_blits) -> None:
    if not _info_cluster_compose_active(now_mono):
        return
    _warm_info_cluster_blits(now_mono)
    if not info_cluster_blits[0] or alpha_blend_bgra_over_bgr is None:
        return
    for ib in info_cluster_blits[0]:
        x0b, y0b, wwb, whb = int(ib.x), int(ib.y), int(ib.w), int(ib.h)
        x2, y2, rw2, rh2 = _design_rect_to_target(x0b, y0b, wwb, whb, cap_w, cap_h)
        _ph2, _pw2 = ib.bgra.shape[:2]
        patch = cv2.resize(
            ib.bgra,
            (rw2, rh2),
            interpolation=cv_resize_interp(_pw2, _ph2, rw2, rh2),
        )
        sub = target[y2 : y2 + rh2, x2 : x2 + rw2]
        sub[:] = alpha_blend_bgra_over_bgr(sub, patch)


def sync_developer_chrome(*, DevPhase, _PIGEON_EXT, _apply_dev_phase_widgets, _layout_chrome, _settings_unbind_wheel_globals, _start_location_toast, command_bar, command_entry_visible, dev_phase, hide_command_entry, label, place_command_bar, prev_dev_phase_for_location_toast, root) -> None:
    was_phase = prev_dev_phase_for_location_toast[0]
    _apply_dev_phase_widgets()
    _layout_chrome()
    if dev_phase[0] == DevPhase.GRID:
        root.title(f"Pigeon {version_string()} — Developer mode (grid)")
        label.configure(
            highlightthickness=3,
            highlightbackground="#0a84ff",
            highlightcolor="#0a84ff",
        )
    elif dev_phase[0] == DevPhase.MAIN_SETTINGS:
        root.title(f"Pigeon {version_string()} — settings")
        try:
            label.configure(highlightthickness=0)
        except tk.TclError:
            pass
    else:
        root.title("")
        label.configure(highlightthickness=0)
        hide_command_entry()
    _settings_unbind_wheel_globals()
    if command_entry_visible[0]:
        place_command_bar()
        command_bar.lift()
    if _PIGEON_EXT and dev_phase[0] == DevPhase.OFF and was_phase != DevPhase.OFF:
        # Keep the same launch placement after leaving Settings.
        _start_location_toast(startup=True)
    prev_dev_phase_for_location_toast[0] = dev_phase[0]


def _on_advanced_matrix_closed(*, advanced_matrix_restore_phase, dev_phase, skip_cache, sync_developer_chrome) -> None:
    tgt = advanced_matrix_restore_phase[0]
    if tgt is not None:
        advanced_matrix_restore_phase[0] = None
        dev_phase[0] = tgt  # type: ignore[assignment]
        skip_cache[0] = None
        sync_developer_chrome()


def _refresh_stage_from_poster(*, _PIGEON_EXT, _apply_stage_chrome_colors, black_photo, skip_cache) -> None:
    if not _PIGEON_EXT:
        set_stage_bgr(0, 0, 0)
    else:
        from pigeon.widgets.poster_art import sync_stage_background_from_active_poster

        sync_stage_background_from_active_poster()
    _apply_stage_chrome_colors()
    black_photo[0] = None
    skip_cache[0] = None


def _compose_shown_frame(frame_bgr: np.ndarray | None, brightness: float, *, DESIGN_H, DESIGN_W, DisplayView, SceneFit, ViewOneVariant, _PIGEON_EXT, _PROJECT_DIR, _app_logo_clock_saver_style_now, _apply_brightness, _backdrop_active_for_view, _black_screen_bgr, _clock_saver_for_compose, _composite_cap_dims, _current_app_display_name, _current_view_one_variant, _design_grid_overlay_active, _effective_display_view, _paste_bgra_contain_on_design, _playback_display_title, _present_frame_to_display, _resolve_streaming_app_logo_bgra, _view_one_is_pigeon_poster, _view_one_variant_uses_full_path, _view_one_variant_uses_simple_path, _view_one_video_content_a_tt_contain_rect_design, _vv_has_content_title, _vv_has_tmdb_bd, _vv_is_music, _vv_music_text_lines, backdrop_app_logo_letterbox_fit, backdrop_master_bgr, compose_display_fast_no_grid, compose_display_from_source, display_dims, load_pigeon_temp_logo_bgra, render_ui_music_text_patch_bgra, render_ui_text_patch_bgra, status_bar_widget, view_circles_widget) -> np.ndarray:
    if (
        _PIGEON_EXT
        and view_circles_widget is not None
        and _effective_display_view() == DisplayView.ONE
    ):
        return compose_display_fast_no_grid(
            frame_bgr,
            brightness,
            frame_is_display_sized=bool(
                frame_bgr is not None
                and frame_bgr.size > 0
                and int(frame_bgr.shape[0]) == int(DESIGN_H)
                and int(frame_bgr.shape[1]) == int(DESIGN_W)
            ),
        )

    def _view_one_dark_accent_bg_bgr() -> tuple[int, int, int]:
        """Darker variant of the current accent color for viewOne video a/c backgrounds.

        Stays black until ``pigeonTMDB_BD`` is ready (so the accent is actually
        sampled from a real backdrop, not the orange fallback).
        """
        if not _vv_has_tmdb_bd():
            return (0, 0, 0)
        if status_bar_widget is None:
            return (0, 0, 0)
        base = tuple(int(v) & 255 for v in status_bar_widget.accent_bgr)
        # Keep the hue but darken enough to sit behind TT/poster overlays.
        darken = 0.42
        return (
            int(round(base[0] * darken)),
            int(round(base[1] * darken)),
            int(round(base[2] * darken)),
        )

    if _PIGEON_EXT and _effective_display_view() == DisplayView.FOUR:
        return _black_screen_bgr()
    if (
        _PIGEON_EXT
        and _effective_display_view() != DisplayView.ONE
        and _view_one_is_pigeon_poster()
        and not _vv_is_music()
        and _vv_has_content_title()
    ):
        # viewOne.videoContent_c: black base + active TMDb poster only
        # (no pigeonTMDB_TT / no pigeonTMDB_BD), with the same chrome stack
        # as the other View One layouts. Poster occupies y=[0, top(row 7)].
        sb, sg, sr = _view_one_dark_accent_bg_bgr()
        black = np.empty((DESIGN_H, DESIGN_W, 3), dtype=np.uint8)
        black[:] = (sb, sg, sr)
        return compose_display_from_source(
            black,
            brightness,
            show_grid=_design_grid_overlay_active(),
            frame_is_design_sized=True,
        )
    if (
        _PIGEON_EXT
        and _effective_display_view() != DisplayView.ONE
        and _view_one_variant_uses_simple_path()
        and not _backdrop_active_for_view()
    ):
        sb, sg, sr = _view_one_dark_accent_bg_bgr()
        black = np.empty((DESIGN_H, DESIGN_W, 3), dtype=np.uint8)
        black[:] = (sb, sg, sr)
        sub2_logo_rect = _view_one_video_content_a_tt_contain_rect_design()
        # MediaType.Music override (viewOne.01): TMDb doesn't index music
        # tracks, so no pigeonTMDB_TT is available. Substitute a two-line
        # text patch (track title large; "Artist - Album" smaller beneath)
        # inside the same rect pigeonTMDB_TT would occupy. Short-circuits
        # the V# resolver dispatch below so Music content consistently
        # renders text regardless of which fallback variant would otherwise
        # apply. The single-small-line case in ``render_ui_music_text_patch_bgra``
        # gives the title ~76% of the box height and the subtitle the rest.
        if _vv_is_music() and render_ui_music_text_patch_bgra is not None:
            _m_title, _m_subtitle = _vv_music_text_lines()
            if _m_title or _m_subtitle:
                _music_bgra = render_ui_music_text_patch_bgra(
                    _m_title,
                    _m_subtitle,
                    "",
                    int(sub2_logo_rect[2]),
                    int(sub2_logo_rect[3]),
                )
                if _music_bgra is not None:
                    _paste_bgra_contain_on_design(
                        black, _music_bgra, sub2_logo_rect
                    )
                    return compose_display_from_source(
                        black,
                        brightness,
                        show_grid=_design_grid_overlay_active(),
                        frame_is_design_sized=True,
                    )
        # Variant-aware TT-slot content. V01/V04 draw the real pigeonTMDB_TT via
        # compose_display_from_source (post–v0.6.14 swap: V01 is the TT-only default,
        # V04 is the BD-missing alternate); V06/.07/.08/.09 substitute a generated
        # patch (title text / appLogo / app name / pigeonTempLogo).
        _vv_simple = _current_view_one_variant()
        _vv_use_default_tt = ViewOneVariant is None or _vv_simple in (
            ViewOneVariant.V01,
            ViewOneVariant.V04,
        )
        if not _vv_use_default_tt:
            _override_bgra = None
            if _vv_simple == ViewOneVariant.V06 and render_ui_text_patch_bgra is not None:
                _override_bgra = render_ui_text_patch_bgra(
                    _playback_display_title(),
                    int(sub2_logo_rect[2]),
                    int(sub2_logo_rect[3]),
                )
            elif _vv_simple == ViewOneVariant.V07:
                _override_bgra = _resolve_streaming_app_logo_bgra()
            elif _vv_simple == ViewOneVariant.V08 and render_ui_text_patch_bgra is not None:
                _override_bgra = render_ui_text_patch_bgra(
                    _current_app_display_name(),
                    int(sub2_logo_rect[2]),
                    int(sub2_logo_rect[3]),
                )
            elif _vv_simple == ViewOneVariant.V09 and load_pigeon_temp_logo_bgra is not None:
                # Keep startup/no-content logo treatment centered, matching other app-logo
                # presentations, while preserving the same max slot size.
                _rw = max(1, int(sub2_logo_rect[2]))
                _rh = max(1, int(sub2_logo_rect[3]))
                _rx = max(0, (int(DESIGN_W) - _rw) // 2)
                _ry = max(0, (int(DESIGN_H) - _rh) // 2)
                sub2_logo_rect = (_rx, _ry, _rw, _rh)
                _override_bgra = load_pigeon_temp_logo_bgra(
                    Path(_PROJECT_DIR) / "pigeonAssets"
                )
                if _override_bgra is None:
                    print(
                        "pigeon: pigeonAssets/App logos/AppLogo_Pigeon.png not found — "
                        "viewOne.noContent will render black only.",
                        file=sys.stderr,
                    )
                else:
                    # viewOne.noContent: Pigeon logo at 30% opacity.
                    _override_bgra = _override_bgra.copy()
                    _override_bgra[..., 3] = (
                        _override_bgra[..., 3].astype(np.float32) * 0.30
                    ).clip(0, 255).astype(np.uint8)
            _v07_skip_tt_for_clock_saver = (
                _vv_simple == ViewOneVariant.V07
                and _clock_saver_for_compose(time.monotonic())
            )
            if not _v07_skip_tt_for_clock_saver:
                _paste_bgra_contain_on_design(
                    black, _override_bgra, sub2_logo_rect
                )
            return compose_display_from_source(
                black,
                brightness,
                show_grid=_design_grid_overlay_active(),
                frame_is_design_sized=True,
            )
        return compose_display_from_source(
            black,
            brightness,
            show_grid=_design_grid_overlay_active(),
            frame_is_design_sized=True,
            tmdb_logo_cover_design_xywh=sub2_logo_rect,
        )
    # View 2 backdrop + visualizer-only is handled in ``compose_display_fast_no_grid``.
    # View 1 pigeonFull also uses that backdrop fast path.
    if (
        _backdrop_active_for_view()
        and backdrop_master_bgr[0] is not None
        and _effective_display_view() != DisplayView.TWO
        and not _view_one_variant_uses_full_path()
    ):
        from pigeon.image_ui_protocol import build_backdrop_design_layer_bgr

        if not _PIGEON_EXT:
            # Legacy path: use backdrop-only display if extension isn't available.
            bd = build_backdrop_design_layer_bgr(
                backdrop_master_bgr[0],
                app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit[0],
                app_logo_clock_saver_style=_app_logo_clock_saver_style_now(),
            )
            return compose_display_from_source(bd, brightness, show_grid=False, frame_is_design_sized=True)
        bd = build_backdrop_design_layer_bgr(
            backdrop_master_bgr[0],
            app_logo_letterbox_fit=backdrop_app_logo_letterbox_fit[0],
            app_logo_clock_saver_style=_app_logo_clock_saver_style_now(),
        )
        return compose_display_from_source(
            bd,
            brightness,
            show_grid=_design_grid_overlay_active(),
            frame_is_design_sized=True,
        )

    if not _PIGEON_EXT:
        if frame_bgr is None or frame_bgr.size == 0:
            return _black_screen_bgr()
        lit = _apply_brightness(frame_bgr, brightness)
        dw, dh = display_dims[0], display_dims[1]
        cw, ch, cap_down = _composite_cap_dims(dw, dh)
        small = SceneFit(target_w=cw, target_h=ch).scale_and_crop(lit)
        if cap_down:
            return _present_frame_to_display(small, dw, dh)
        return small
    if _PIGEON_EXT and _design_grid_overlay_active():
        return compose_display_from_source(frame_bgr, brightness, show_grid=True)
    return compose_display_fast_no_grid(frame_bgr, brightness)


def _backdrop_active_for_view(*, DisplayView, _effective_display_view, use_backdrop_scene) -> bool:
    """True when backdrop scene should be used by the current effective view."""
    return bool(use_backdrop_scene[0] and _effective_display_view() != DisplayView.SIX)
