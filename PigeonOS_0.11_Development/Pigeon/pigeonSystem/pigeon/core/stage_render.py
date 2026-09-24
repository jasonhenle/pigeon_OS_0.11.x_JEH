"""Stage / frame rendering helpers and render scheduling.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

import tkinter as tk
import cv2
import numpy as np
from pigeon.stage_background import bgr_to_tk_hex
from pigeon.stage_background import get_stage_bgr


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
