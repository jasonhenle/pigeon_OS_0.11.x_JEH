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

if TYPE_CHECKING:
    from pigeon_0_9 import SceneFit


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
