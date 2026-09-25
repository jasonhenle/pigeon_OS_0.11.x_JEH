"""Splash / startup choreography: clock underlay capture and the post-splash clock fade-in.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

import numpy as np
import time
import tkinter as tk


def _early_splash_clock_underlay(*, _bgr_to_tk_image, _early_clock_underlay_photo, _reveal_clock_under_splash, _splash_reveal_clock, _splash_underlay_bgr, label) -> None:
    """Copy clock underlay onto the real video label (under splash / after lift)."""
    if not _splash_reveal_clock[0]:
        return
    under = _splash_underlay_bgr[0]
    if under is None:
        _reveal_clock_under_splash(refresh=False)
        under = _splash_underlay_bgr[0]
    if under is None:
        return
    try:
        _early_clock_underlay_photo[0] = _bgr_to_tk_image(under)
        label.configure(image=_early_clock_underlay_photo[0])
        label.image = _early_clock_underlay_photo[0]
    except Exception:
        pass


def _clock_startup_intro_opacity(now: float, *, CLOCK_STARTUP_FADE_S, clock_saver_composite_bgra, post_splash_mono, startup_ph) -> float | None:
    """Smooth 0→1 while the post-splash clock is the first reveal; else ``None``."""
    t0 = post_splash_mono[0]
    if t0 is None or startup_ph[0] is not None:
        return None
    if clock_saver_composite_bgra is None:
        return None
    elapsed = now - float(t0)
    if elapsed >= float(CLOCK_STARTUP_FADE_S):
        return None
    u = max(0.0, min(1.0, elapsed / max(1e-6, float(CLOCK_STARTUP_FADE_S))))
    return u * u * (3.0 - 2.0 * u)


def _capture_splash_underlay(out_bgr: np.ndarray, *, WINDOW_H, WINDOW_W, _present_frame_to_display, _splash_underlay_bgr, startup_ph) -> None:
    """Keep a window-sized UI snapshot for the splash fade unveil."""
    if startup_ph[0] is None or out_bgr is None or out_bgr.size == 0:
        return
    try:
        if out_bgr.shape[0] == WINDOW_H and out_bgr.shape[1] == WINDOW_W:
            _splash_underlay_bgr[0] = np.ascontiguousarray(out_bgr)
        else:
            _splash_underlay_bgr[0] = np.ascontiguousarray(
                _present_frame_to_display(
                    out_bgr, WINDOW_W, WINDOW_H, native_now_playing=True
                )
            )
    except Exception:
        pass


def _warm_view_one_splash_chrome_only(*, phase: str = "chrome-only", DisplayView, _PIGEON_EXT, _log_view_one_startup_phase, display_view_holder, root, view_circles_widget) -> None:
    """Rasterize View 1 SVG chrome early (no playback poll helpers required)."""
    if not _PIGEON_EXT or view_circles_widget is None:
        return
    t0 = time.monotonic()
    display_view_holder[0] = DisplayView.ONE
    # Clock-only until content is live — do not force the empty status bar on.
    if view_circles_widget.set_now_playing_chrome_visible(True):
        view_circles_widget.clear_cache()
    try:
        view_circles_widget.bgra_frame()
    except Exception:
        pass
    try:
        root.update_idletasks()
    except tk.TclError:
        pass
    _log_view_one_startup_phase(f"{phase} ({(time.monotonic() - t0) * 1000.0:.0f} ms raster)")


def _warm_view_one_under_splash(*, phase: str = "full-warm", _PIGEON_EXT, _enable_now_playing_screen, _log_view_one_startup_phase, _splash_view_one_warm_done, _warm_status_bar_blits, root, view_circles_widget) -> None:
    """Full View 1 enable + state sync once playback helpers exist."""
    if not _PIGEON_EXT:
        return
    t0 = time.monotonic()
    _enable_now_playing_screen()
    _warm_status_bar_blits()
    try:
        if view_circles_widget is not None:
            view_circles_widget.bgra_frame()
    except Exception:
        pass
    try:
        root.update_idletasks()
    except tk.TclError:
        pass
    _splash_view_one_warm_done[0] = True
    _log_view_one_startup_phase(f"{phase} ({(time.monotonic() - t0) * 1000.0:.0f} ms)")
