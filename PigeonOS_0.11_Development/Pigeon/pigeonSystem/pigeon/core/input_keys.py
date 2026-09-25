"""Keyboard / mouse / window event handlers.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

import time
import tkinter as tk
import sys
from pigeon.runtime_paths import PIGEON_STATE_DIR_TILDE
import tkinter.messagebox as messagebox


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


def on_tmdb_retry_hotkey(event: tk.Event, *, _PIGEON_EXT, _bump_pigeon_user_activity, _last_tmdb_hotkey_mono, _perform_tmdb_artwork_retry, _widget_accepts_typing) -> str | None:
    _bump_pigeon_user_activity(event)
    if not _PIGEON_EXT:
        return None
    if _widget_accepts_typing(event.widget):
        return None
    now_hk = time.monotonic()
    if now_hk - _last_tmdb_hotkey_mono[0] < 0.15:
        return "break"
    _last_tmdb_hotkey_mono[0] = now_hk
    _perform_tmdb_artwork_retry()
    return "break"


def on_ctrl_shift_tab_advanced(event: tk.Event, *, _PIGEON_EXT, _bump_pigeon_user_activity, _last_adv_shift_tab_mono, _open_advanced_capability_matrix, _widget_accepts_typing) -> str | None:
    if _widget_accepts_typing(event.widget):
        return None
    if not _PIGEON_EXT:
        return None
    st = int(getattr(event, "state", 0))
    if not (st & 0x0004) or not (st & 0x0001):
        return None
    _bump_pigeon_user_activity(event)
    now = time.monotonic()
    if now - _last_adv_shift_tab_mono[0] < 0.35:
        return "break"
    _last_adv_shift_tab_mono[0] = now
    _open_advanced_capability_matrix()
    return "break"


def on_tmdb_match_mode_toggle(event: tk.Event, *, _PIGEON_EXT, _bump_pigeon_user_activity, _last_tmdb_match_toggle_mono, _widget_accepts_typing) -> str | None:
    _bump_pigeon_user_activity(event)
    if not _PIGEON_EXT:
        return None
    if _widget_accepts_typing(event.widget):
        return None
    now_tm = time.monotonic()
    if now_tm - _last_tmdb_match_toggle_mono[0] < 0.2:
        return "break"
    _last_tmdb_match_toggle_mono[0] = now_tm
    try:
        from pigeon.tmdb_poster import toggle_tmdb_match_mode
    except ImportError:
        return None
    mode = toggle_tmdb_match_mode()
    sys.stderr.write(f"pigeon: TMDb title match: {mode} (Ctrl+Shift+M to toggle)\n")
    sys.stderr.flush()
    return "break"


def _on_volume_rotary_action(action: str, *, _bump_pigeon_user_activity, _note_zone3_volume_takeover, _nudge_clock_saver_volume, _queue_receiver_volume_action, _volume_rotary_fail_log_count, apple_tv_busy, current_apple_tv, receiver_power_on_pending, receiver_power_on_until, receiver_standby_holder, streaming_slot_holder) -> None:
    _bump_pigeon_user_activity()
    if action not in ("volume_up", "volume_down", "mute_toggle"):
        return
    _note_zone3_volume_takeover()
    try:
        if receiver_standby_holder[0] or receiver_power_on_pending[0]:
            receiver_power_on_pending[0] = True
            receiver_power_on_until[0] = time.monotonic() + 12.0
            receiver_standby_holder[0] = False
            from pigeon.runtime_state import update_receiver_runtime

            update_receiver_runtime(standby=False, reachable=True)
    except Exception:
        pass
    if _queue_receiver_volume_action(action):
        _nudge_clock_saver_volume(action)
        return
    try:
        from pigeon.player_remote import queue_player_remote_action

        ok = queue_player_remote_action(
            streaming_slot_holder[0],
            current_apple_tv=current_apple_tv,
            action=action,
            apple_tv_busy=apple_tv_busy,
        )
    except Exception as exc:
        ok = False
        if _volume_rotary_fail_log_count[0] < 8:
            sys.stderr.write(f"pigeon: rotary_volume_gpio: {action} failed: {exc}\n")
            sys.stderr.flush()
            _volume_rotary_fail_log_count[0] += 1
    if not ok and _volume_rotary_fail_log_count[0] < 8:
        sys.stderr.write(
            f"pigeon: rotary_volume_gpio: no player remote command for {action!r}\n"
        )
        sys.stderr.flush()
        _volume_rotary_fail_log_count[0] += 1
    _nudge_clock_saver_volume(action)


def _on_play_pause_gpio_action(*, _bump_pigeon_user_activity, _play_pause_gpio_last_mono, _send_player_play_pause_hotkey) -> None:
    _bump_pigeon_user_activity()
    now_pp = time.monotonic()
    if now_pp - _play_pause_gpio_last_mono[0] < 0.25:
        return
    _play_pause_gpio_last_mono[0] = now_pp
    ok = _send_player_play_pause_hotkey()
    try:
        sys.stderr.write(
            "pigeon: play_pause_gpio: "
            + ("sent play_pause\n" if ok else "no player remote command\n")
        )
        sys.stderr.flush()
    except Exception:
        pass


def on_dev_series_title_training_hotkey(event: tk.Event, *, DevPhase, DisplayView, _PIGEON_EXT, _bump_pigeon_user_activity, _widget_accepts_typing, apple_tv_auto_state, dev_phase, display_view_holder, root, spawn_tmdb_poster_fetch) -> str | None:
    """Dev-only: map current playback metadata fingerprint → series title (training JSON)."""
    _bump_pigeon_user_activity(event)
    if not _PIGEON_EXT:
        return None
    if dev_phase[0] != DevPhase.GRID and display_view_holder[0] != DisplayView.FIVE:
        return None
    if _widget_accepts_typing(event.widget):
        return None
    lm = apple_tv_auto_state.get("last_metadata")
    if not isinstance(lm, dict) or not any(
        str(lm.get(k) or "").strip()
        for k in ("title", "series_name", "artist", "album", "query")
    ):
        messagebox.showinfo(
            "Series title training",
            "No playback metadata snapshot yet. Start playback and wait for a poll, then try again.",
            parent=root,
        )
        return "break"
    try:
        from pigeon.raw_title import raw_title_from_metadata_dict
        from pigeon.series_title_training import add_training_mapping
    except ImportError:
        messagebox.showinfo(
            "Series title training",
            "Training modules are not available in this build.",
            parent=root,
        )
        return "break"

    rt = raw_title_from_metadata_dict(lm)
    sig = rt.training_signature_normalized()
    if not sig:
        messagebox.showinfo(
            "Series title training",
            "Could not build a stable fingerprint from the current metadata.",
            parent=root,
        )
        return "break"

    tw = tk.Toplevel(root)
    tw.title("Series title training")
    tw.transient(root)
    tk.Label(
        tw,
        text="Map this playback fingerprint to a TMDb series title.\n"
        f"Saved under {PIGEON_STATE_DIR_TILDE}/series_title_training_hints.json",
        justify="center",
    ).pack(padx=12, pady=(10, 4))
    preview = sig[:180] + ("…" if len(sig) > 180 else "")
    tk.Label(
        tw,
        text=f"Key: {preview}",
        fg="#888",
        wraplength=420,
        justify="left",
    ).pack(padx=12, pady=4)
    ent = tk.Entry(tw, width=48)
    ent.pack(padx=12, pady=6)
    hint = (rt.layer_series_title or rt.raw_series_name or rt.raw_title or "").strip()
    if hint:
        ent.insert(0, hint)

    def _save_training() -> None:
        q_sp = ent.get().strip()
        ok_h, msg_h = add_training_mapping(sig, q_sp)
        if ok_h:
            sys.stderr.write(f"pigeon: series title training: {msg_h}\n")
            sys.stderr.flush()
            tw.destroy()
            if q_sp:
                spawn_tmdb_poster_fetch(q_sp, prefer=str(apple_tv_auto_state.get("prefer") or "auto"), force=True)
        else:
            messagebox.showerror("Series title training", msg_h, parent=tw)

    bf = tk.Frame(tw)
    bf.pack(pady=(4, 12))
    tk.Button(bf, text="Save & refetch TMDb", command=_save_training).pack(side=tk.LEFT, padx=6)
    tk.Button(bf, text="Cancel", command=tw.destroy).pack(side=tk.LEFT, padx=6)
    root.after_idle(lambda: ent.focus_set())
    return "break"


def on_escape(event: tk.Event, *, _bump_pigeon_user_activity, command_entry_visible, hide_command_entry, quit_app) -> str | None:
    _bump_pigeon_user_activity(event)
    if command_entry_visible[0]:
        hide_command_entry()
        return "break"
    quit_app()
    return "break"
