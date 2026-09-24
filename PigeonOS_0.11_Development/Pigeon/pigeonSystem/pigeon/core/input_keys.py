"""Keyboard / mouse / window event handlers.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

import time
import tkinter as tk


def quit_app(_event=None, *, root) -> None:
    root.quit()


def try_cycle_dev_phase(event: tk.Event | None, *, _last_overlay_mono, cycle_dev_phase) -> str | None:
    """Debounced OFF↔MAIN_SETTINGS cycle (mouse / F9); design grid overlay is key 5."""
    now = time.monotonic()
    if now - _last_overlay_mono[0] < 0.08:
        return "break"
    _last_overlay_mono[0] = now
    cycle_dev_phase()
    return "break"


def on_tab_key(event: tk.Event, *, _last_overlay_mono, cycle_dev_phase) -> str | None:
    """Tab / Shift+Tab / Ctrl+Tab: toggle OFF ↔ MAIN_SETTINGS.

    Tk on X11 (Pi) binds class ``<Tab>`` to focus traversal and returns
    break before ``bind_all``, so this handler is also installed on
    Label/Button/Entry and the other traversal classes.
    """
    st_tab = int(getattr(event, "state", 0) or 0)
    # Ctrl+Shift+Tab opens the advanced matrix (extension build).
    if (st_tab & 0x0004) and (st_tab & 0x0001):
        return None
    now = time.monotonic()
    if now - _last_overlay_mono[0] < 0.08:
        return "break"
    _last_overlay_mono[0] = now
    cycle_dev_phase()
    return "break"


def on_shift_tab_dev_cycle(event: tk.Event, *, on_tab_key) -> str | None:
    """Shift+Tab: same settings toggle as Tab."""
    return on_tab_key(event)


def on_ctrl_tab(event: tk.Event, *, on_tab_key) -> str | None:
    return on_tab_key(event)


def on_s_key(event: tk.Event, *, _design_grid_overlay_active, _last_s_mono, toggle_scene) -> str | None:
    keysym = (getattr(event, "keysym", "") or "").lower()
    ch = (getattr(event, "char", "") or "").lower()
    if keysym != "s" and ch != "s":
        return None
    if not _design_grid_overlay_active():
        return None
    now = time.monotonic()
    if now - _last_s_mono[0] < 0.08:
        return "break"
    _last_s_mono[0] = now
    toggle_scene(require_overlay=True)
    return "break"


def on_f10_key(_event: tk.Event | None = None, *, _design_grid_overlay_active, _last_f10_mono, f10_cycle_scene_grid, toggle_scene) -> str:
    now = time.monotonic()
    if now - _last_f10_mono[0] < 0.12:
        return "break"
    _last_f10_mono[0] = now
    if _design_grid_overlay_active():
        f10_cycle_scene_grid()
    else:
        toggle_scene(require_overlay=False)
    return "break"


def on_click_focus(_event: tk.Event | None = None, *, _bump_pigeon_user_activity, label, root) -> None:
    _bump_pigeon_user_activity(_event)
    try:
        label.focus_set()
    except tk.TclError:
        try:
            root.focus_set()
        except tk.TclError:
            pass


def on_double_click_scene(_event: tk.Event | None = None, *, toggle_scene) -> None:
    toggle_scene(require_overlay=False)


def _on_par_chord_release(event: tk.Event, *, _par_chord_fired, _par_chord_held) -> str | None:
    ks = (getattr(event, "keysym", "") or "").lower()
    if ks in ("p", "a", "r"):
        _par_chord_held.discard(ks)
    if not _par_chord_held:
        _par_chord_fired[0] = False
    return None


def _focus_when_mapped(_event=None, *, root) -> None:
    try:
        root.focus_force()
    except tk.TclError:
        root.focus_set()


def _on_shell_configure(event: tk.Event, *, _apply_shell_size, shell) -> None:
    if event.widget is not shell:
        return
    w, h = int(event.width), int(event.height)
    # Apply on every configure so chrome (buttons, HUD, bars) tracks live resize;
    # debounced after() only ran after drag ended.
    _apply_shell_size(w, h)
