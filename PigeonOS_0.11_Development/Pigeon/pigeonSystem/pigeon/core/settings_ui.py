"""Settings panel helpers: scrolling, wheel binding, device-row parsing, pairing LEDs, tooltips and update-check UI.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

from pathlib import Path
import numpy as np
import time
import tkinter as tk
import tkinter.messagebox as messagebox
from pigeon.app_state import read_all_locations_v2
from pigeon.app_state import read_app_state
from pigeon.app_state import read_current_location_id
from pigeon.media_folders import pigeon_pulled_media_dir
from pigeon.media_folders import pigeon_reformatted_media_dir
from pigeon.media_folders import purge_directory_contents
from pigeon.app_state import delete_location_v2
from pigeon.app_state import row_is_playback_apple_tv
from pigeon.version import version_string
import sys
from pigeon.app_state import clear_last_apple_tv
from pigeon.app_state import read_saved_streaming_device
from pigeon.app_state import write_saved_streaming_device
from pigeon.app_state import clear_last_receiver
from pigeon.app_state import read_saved_av_receiver
from pigeon.app_state import write_saved_av_receiver
import threading
from pigeon.app_state import add_empty_location_v2
from pigeon.app_state import clear_all_persisted_devices_and_targets
from pigeon.app_state import read_current_location_name
from pigeon.app_state import rename_location_v2
from pigeon.app_state import set_current_location_id
from pigeon.app_state import write_location_wifi
from pigeon.runtime_paths import pigeon_state_dir
import os
import queue
import tkinter.simpledialog as simpledialog


def _settings_update_scrollregion(event: tk.Event | None = None, *, _settings_inner_scroll_size, settings_canvas, settings_inner) -> None:
    try:
        if event is not None and getattr(event, "widget", None) is not settings_inner:
            return
        if event is not None:
            settings_inner.update_idletasks()
            rw = int(settings_inner.winfo_reqwidth())
            rh = int(settings_inner.winfo_reqheight())
            if rw == _settings_inner_scroll_size[0] and rh == _settings_inner_scroll_size[1]:
                return
            _settings_inner_scroll_size[0] = rw
            _settings_inner_scroll_size[1] = rh
        settings_canvas.update_idletasks()
        bbox = settings_canvas.bbox("all")
        if bbox:
            settings_canvas.configure(scrollregion=bbox)
    except tk.TclError:
        pass


def _settings_on_canvas_configure(event: tk.Event, *, _settings_inner_win, _settings_update_scrollregion, root, settings_canvas) -> None:
    try:
        inner_w = max(1, int(event.width) - 32)
        settings_canvas.itemconfig(_settings_inner_win, width=inner_w)
    except tk.TclError:
        pass
    root.after_idle(lambda: _settings_update_scrollregion(None))


def _settings_wheel_target_should_ignore(widget: tk.Misc) -> bool:
    """Let Listbox/Text/Entry keep their own scroll behavior."""
    try:
        w: tk.Misc | None = widget
        while w is not None:
            cls = w.winfo_class()
            if cls in ("Listbox", "Text", "Entry", "TEntry", "TCombobox"):
                return True
            master = w.master
            w = master if isinstance(master, tk.Misc) else None
    except tk.TclError:
        pass
    return False


def _settings_is_under_scroll_surface(widget: tk.Misc, *, settings_scroll_outer) -> bool:
    """True when ``widget`` is the settings canvas, scrollbar, or any descendant."""
    try:
        w: tk.Misc | None = widget
        while w is not None:
            if w is settings_scroll_outer:
                return True
            master = w.master
            w = master if isinstance(master, tk.Misc) else None
    except tk.TclError:
        pass
    return False


def _settings_bind_wheel_globals(*, _settings_mousewheel, root, settings_wheel_all_bound) -> None:
    if settings_wheel_all_bound[0]:
        return
    root.bind_all("<MouseWheel>", _settings_mousewheel)
    root.bind_all("<Button-4>", _settings_mousewheel)
    root.bind_all("<Button-5>", _settings_mousewheel)
    settings_wheel_all_bound[0] = True


def _settings_unbind_wheel_globals(*, root, settings_wheel_all_bound) -> None:
    if not settings_wheel_all_bound[0]:
        return
    try:
        root.unbind_all("<MouseWheel>")
        root.unbind_all("<Button-4>")
        root.unbind_all("<Button-5>")
    except tk.TclError:
        pass
    settings_wheel_all_bound[0] = False


def on_purge_image_media(*, root) -> None:
    if not messagebox.askokcancel(
        "Purge image media",
        "Delete all files in pigeonPulledMedia and pigeonReformattedMedia?",
        parent=root,
    ):
        return
    ok1, msg1 = purge_directory_contents(pigeon_pulled_media_dir())
    ok2, msg2 = purge_directory_contents(pigeon_reformatted_media_dir())
    if ok1 and ok2:
        messagebox.showinfo("Purge image media", f"{msg1}\n{msg2}")
    else:
        messagebox.showerror("Purge image media", f"{msg1}\n{msg2}")


def _on_location_name_return(_event: tk.Event, *, _apply_location_rename) -> str:
    _apply_location_rename()
    return "break"


def _register_tmdb_adv_widgets(manual: tk.Button, report: tk.Button, log_w: tk.Misc, *, tmdb_adv_log_text_holder, tmdb_adv_manual_btn_holder, tmdb_adv_report_btn_holder) -> None:
    tmdb_adv_manual_btn_holder[0] = manual
    tmdb_adv_report_btn_holder[0] = report
    tmdb_adv_log_text_holder[0] = log_w


def _unregister_tmdb_adv_widgets(*, tmdb_adv_log_text_holder, tmdb_adv_manual_btn_holder, tmdb_adv_report_btn_holder) -> None:
    tmdb_adv_manual_btn_holder[0] = None
    tmdb_adv_report_btn_holder[0] = None
    tmdb_adv_log_text_holder[0] = None


def _append_tmdb_retry_log_ui(line: str, *, tmdb_adv_log_text_holder) -> None:
    w = tmdb_adv_log_text_holder[0]
    if w is None:
        return
    try:
        w.insert(tk.END, line + "\n")
        w.see(tk.END)
        body = w.get("1.0", "end-1c")
        if body.count("\n") > 130:
            w.delete("1.0", "31.0")
    except tk.TclError:
        pass


def _match_neighbor_button_style(btn: tk.Button, *, ref: tk.Button) -> None:
    """Copy default macOS/system button chrome from a sibling (Find device / Advanced)."""
    for key in (
        "bg",
        "fg",
        "activebackground",
        "activeforeground",
        "highlightbackground",
        "highlightcolor",
        "highlightthickness",
        "relief",
        "borderwidth",
        "disabledforeground",
    ):
        try:
            btn.configure(**{key: ref.cget(key)})
        except tk.TclError:
            pass


def _resolve_install_root_for_update() -> Path:
    install_root = Path(__file__).resolve().parent.parent
    try:
        from pigeon.github_update import resolve_install_root

        resolved = resolve_install_root(script_path=__file__)
        if resolved is not None:
            install_root = resolved
    except Exception:
        pass
    return install_root


def _finish_update_check(result: object, *, _sync_update_button_style, update_check_state) -> None:
    update_check_state["checking"] = False
    update_check_state["last_check_mono"] = time.monotonic()
    try:
        from pigeon.update_check import UpdateCheckResult

        if isinstance(result, UpdateCheckResult):
            update_check_state["update_available"] = bool(result.update_available)
            update_check_state["remote_version"] = result.remote_version
            update_check_state["github_branch"] = result.github_branch
            update_check_state["error"] = result.error
    except Exception:
        update_check_state["update_available"] = False
    _sync_update_button_style()


def _current_location_display_name() -> str:
    cid = (read_current_location_id() or "").strip()
    for L in read_all_locations_v2():
        if str(L.get("id") or "") == cid:
            n = str(L.get("name") or "").strip()
            return n or "Room"
    return "Location"


def _paint_pair_led(which: int, ok: bool | None, *, pairing_led_holder) -> None:
    """Pairing LED: green=detected+compatible, amber=detected+unknown/incompatible, red=not detected/not compatible."""
    canvas = pairing_led_holder[which] if 0 <= which < len(pairing_led_holder) else None
    if canvas is None:
        return
    try:
        canvas.delete("all")
        if ok is True:
            fill = "#1fcb5d"
        elif ok is None:
            fill = "#f0ad4e"
        else:
            fill = "#e74c3c"
        canvas.create_oval(2, 2, 12, 12, fill=fill, outline="#151518", width=1)
    except tk.TclError:
        pass


def _paint_cred_led_canvas(canvas: tk.Canvas | None, ok: bool | None) -> None:
    """Canvas LED: green=detected+compatible, amber=detected+unknown/incompatible, red=not detected/not compatible."""
    if canvas is None:
        return
    try:
        canvas.delete("all")
        if ok is True:
            fill = "#1fcb5d"
        elif ok is None:
            fill = "#f0ad4e"
        else:
            fill = "#e74c3c"
        canvas.create_oval(2, 2, 12, 12, fill=fill, outline="#151518", width=1)
    except tk.TclError:
        pass


def _paired_box_close_button(parent: tk.Frame, bg: str, command: object, *, _S, _kiosk_on) -> tk.Button:
    return tk.Button(
        parent,
        text="\u00d7",
        command=command,
        font=(_S, 14, "normal"),
        fg="#888",
        bg=bg,
        activebackground=bg,
        activeforeground="#f0f0f0",
        bd=0,
        padx=6,
        pady=0,
        highlightthickness=0,
        cursor="none" if _kiosk_on else "hand2",
    )


def _settings_parse_device_rows(raw: object) -> list[dict[str, str]]:
    """Normalize a location slot (list or legacy dict) to filled device rows."""
    if isinstance(raw, list):
        out: list[dict[str, str]] = []
        for x in raw:
            if not isinstance(x, dict):
                continue
            if str(x.get("identifier") or "").strip() and str(x.get("address") or "").strip():
                out.append(dict(x))
        return out
    if isinstance(raw, dict):
        d = dict(raw)
        if str(d.get("identifier") or "").strip() and str(d.get("address") or "").strip():
            return [d]
    return []


def _settings_parse_receiver_rows(raw: object) -> list[dict[str, str]]:
    """Receivers: show any row with a host/IP (identifier optional for legacy)."""
    if isinstance(raw, list):
        return [
            dict(x)
            for x in raw
            if isinstance(x, dict) and str(x.get("address") or "").strip()
        ]
    if isinstance(raw, dict) and str(raw.get("address") or "").strip():
        return [dict(raw)]
    return []


def _refresh_observed_pairing_led_rows(*, _paint_cred_led_canvas, paired_observed_led_last_state, paired_observed_led_rows) -> None:
    if not paired_observed_led_rows:
        return
    try:
        from device_capability_matrix import device_row_stable_key
    except Exception:
        for cv, _lid, _row in paired_observed_led_rows:
            try:
                _paint_cred_led_canvas(cv, False)
            except tk.TclError:
                pass
        return
    try:
        app_state = read_app_state()
    except Exception:
        app_state = {}
    observed_store_by_loc: dict[str, dict[str, object]] = {}
    for cv, lid, row in paired_observed_led_rows:
        try:
            tri_state: bool | None = False
            lid_s = str(lid or "").strip()
            if lid_s:
                if lid_s not in observed_store_by_loc:
                    raw_blob = app_state.get(f"observed_capability_live_v1.{lid_s}")
                    observed_store_by_loc[lid_s] = raw_blob if isinstance(raw_blob, dict) else {}
                blob = observed_store_by_loc.get(lid_s, {})
                sk = str(device_row_stable_key(row) or "").strip()
                if sk:
                    feats = blob.get(sk) if isinstance(blob, dict) else None
                    if isinstance(feats, dict) and feats:
                        has_full = any(
                            isinstance(v, str) and v == "full" for v in feats.values()
                        )
                        tri_state = True if has_full else None
            k = id(cv)
            if paired_observed_led_last_state.get(k, "__missing__") == tri_state:
                continue
            _paint_cred_led_canvas(cv, tri_state)
            paired_observed_led_last_state[k] = tri_state
        except tk.TclError:
            pass


def _device_addr_key(addr: str) -> str:
    s = str(addr or "").strip().lower()
    if not s:
        return ""
    if s.startswith("["):
        return s
    if s.count(":") == 1:
        left, right = s.rsplit(":", 1)
        if right.isdigit():
            return left
    return s.split("%")[0]


def _device_row_matches_saved(row: dict[str, str], saved: dict[str, str], *, _device_addr_key) -> bool:
    ri = str(row.get("identifier") or "").strip()
    si = str(saved.get("identifier") or "").strip()
    if ri and si and ri == si:
        return True
    ra = _device_addr_key(str(row.get("address") or ""))
    sa = _device_addr_key(str(saved.get("address") or ""))
    return bool(ra and sa and ra == sa)


def _save_box_pair_device_row(box_num: int, row: dict[str, str], *, _rebuild_paired_devices_panel, avr_slot_holder, describe_current_apple_tv, receiver_http_host, streaming_slot_holder) -> None:
    from pigeon.app_state import (
        append_device_to_location_slot,
        read_current_location_id,
        read_saved_av_receiver,
        read_saved_streaming_device,
        write_saved_av_receiver,
        write_saved_streaming_device,
    )

    saved = dict(row)
    saved["device_role"] = "player" if box_num == 2 else "receiver"
    lid = read_current_location_id() or None
    if box_num == 2:
        append_device_to_location_slot(
            "streaming",
            saved,
            for_location_id=lid,
            new_location_name=None,
        )
        write_saved_streaming_device(saved, for_location_id=lid)
        streaming_slot_holder[0] = read_saved_streaming_device()
    else:
        write_saved_av_receiver(saved, for_location_id=lid)
        avr_slot_holder[0] = read_saved_av_receiver()
        adr = str((avr_slot_holder[0] or saved).get("address") or "").strip()
        if adr:
            receiver_http_host["host"] = adr
    describe_current_apple_tv()
    _rebuild_paired_devices_panel()


def _attach_hover_tooltip(widget: tk.Misc, message: str, *, S_FONT_SMALL, root) -> None:
    tip: list[tk.Toplevel | None] = [None]

    def show(_event: tk.Event | None = None) -> None:
        if tip[0] is not None:
            return
        tw = tk.Toplevel(root)
        tw.wm_overrideredirect(True)
        try:
            tw.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        x = widget.winfo_rootx() + 4
        y = widget.winfo_rooty() + int(widget.winfo_height()) + 4
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw,
            text=message,
            bg="#2a2a30",
            fg="#e8e8e8",
            font=S_FONT_SMALL,
            padx=8,
            pady=4,
        ).pack()
        tip[0] = tw

    def hide(_event: tk.Event | None = None) -> None:
        if tip[0] is not None:
            tip[0].destroy()
            tip[0] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)


def _settings_mousewheel(event: tk.Event, *, _bump_pigeon_user_activity, _settings_is_under_scroll_surface, _settings_wheel_target_should_ignore, root, settings_canvas, settings_frame) -> str | None:
    _bump_pigeon_user_activity(event)
    # Legacy Tk settings form is never shown.
    if not settings_frame.winfo_ismapped():
        return None
    try:
        under = root.winfo_containing(event.x_root, event.y_root)
    except tk.TclError:
        under = None
    if under is None or not _settings_is_under_scroll_surface(under):
        return None
    if _settings_wheel_target_should_ignore(under):
        return None
    try:
        if sys.platform == "darwin":
            d = int(getattr(event, "delta", 0) or 0)
            if d == 0:
                return "break"
            steps = max(1, abs(d) // 120) if abs(d) >= 120 else 1
            settings_canvas.yview_scroll(-steps if d > 0 else steps, "units")
        else:
            num = int(getattr(event, "num", 0) or 0)
            if num == 4:
                settings_canvas.yview_scroll(-3, "units")
            elif num == 5:
                settings_canvas.yview_scroll(3, "units")
    except tk.TclError:
        pass
    return "break"


def _linux_on_updates_button(*, _run_github_apply_worker, root, update_check_state) -> None:
    """Pi/Linux: skip Python version check; run curl|bash updater from GitHub."""
    if update_check_state.get("applying") or update_check_state.get("checking"):
        return
    local = version_string()
    github_target = "0.8 on GitHub main"
    try:
        from pigeon.update_check import fetch_remote_version_tuple, format_version_tuple

        remote_t, _, _, _ = fetch_remote_version_tuple(
            timeout_s=10.0, force=True
        )
        if remote_t is not None:
            github_target = format_version_tuple(remote_t)
    except Exception:
        pass
    if not messagebox.askyesno(
        "Updates",
        f"Download, install, and restart Pigeon from GitHub?\n\n"
        f"Installed: {local}\n"
        f"GitHub:    {github_target}\n\n"
        f"• Uses curl only (public repo — no GitHub token)\n"
        f"• App code and pigeonAssets are updated to 0.8\n"
        f"• Settings in ~/.pigeon_0_6 are kept\n"
        f"• Pigeon will restart as 0.8 when finished — no further steps\n\n"
        f"Continue?",
        parent=root,
    ):
        return
    _run_github_apply_worker(remote=github_target)


def _begin_apply_update(*, remote: str, branch: object, _run_github_apply_worker, root) -> None:
    local = version_string()
    if not messagebox.askyesno(
        "Install update",
        f"A newer Pigeon is on GitHub.\n\n"
        f"Installed: {local}\n"
        f"Latest:    {remote}\n\n"
        f"Download, install, and restart Pigeon now?\n\n"
        f"• App code and UI assets (pigeonAssets) will be updated from GitHub\n"
        f"• Settings stay in ~/.pigeon_0_6 (devices, TMDb key, pairing)\n"
        f"• Cached TMDb downloads in the app folder are kept\n"
        f"• Pigeon will restart automatically when finished — no further steps",
        parent=root,
    ):
        return

    _run_github_apply_worker(remote=str(remote), branch=str(branch) if branch else None)


def _content_indicator_ok(*, _PIGEON_EXT, apple_tv_auto_state, apple_tv_busy, apple_tv_dashboard_track, current_apple_tv) -> bool:
    if not _PIGEON_EXT:
        return False
    if not current_apple_tv.get("identifier"):
        return False
    if apple_tv_busy["active"]:
        return False
    lf = apple_tv_dashboard_track.get("last_poll_ok")
    # Failed poll must not be masked by a stale latched query.
    if lf is False:
        return False
    q = apple_tv_auto_state.get("query")
    if q:
        return True
    return False


def _advanced_feature_pipeline_ok(*, _PIGEON_EXT, _atv_metadata_is_content_idle, apple_tv_auto_state, apple_tv_busy, apple_tv_dashboard_track, current_apple_tv) -> bool:
    """Stricter than the Content LED: last poll succeeded and metadata is active (not idle/unknown)."""
    if not _PIGEON_EXT:
        return False
    if not current_apple_tv.get("identifier"):
        return False
    if apple_tv_busy["active"]:
        return False
    if apple_tv_dashboard_track.get("last_poll_ok") is not True:
        return False
    lm = apple_tv_auto_state.get("last_metadata")
    if not isinstance(lm, dict) or _atv_metadata_is_content_idle(lm):
        return False
    return True


def _refresh_content_indicator(*, _content_indicator_ok, _paint_boolean_led, _refresh_match_quality_glance_label, content_indicator_cv_holder) -> None:
    cv = content_indicator_cv_holder[0]
    if cv is not None:
        try:
            _paint_boolean_led(cv, _content_indicator_ok())
        except tk.TclError:
            pass
    _refresh_match_quality_glance_label()


def _rebuild_paired_devices_panel(*, S_FONT_CAP_BOLD, S_FONT_SEC, S_FONT_SMALL, _paint_boolean_led, _paint_cred_led_canvas, _paired_box_close_button, _remove_aux_slot_device_at, _remove_receiver_device_at, _remove_streaming_device_at, _settings_parse_device_rows, _settings_parse_receiver_rows, paired_devices_inner, paired_observed_led_last_state, paired_observed_led_rows, paired_ui_leds, receiver_panel_led_holder) -> None:
    paired_ui_leds["remote"] = None
    paired_ui_leds["airplay"] = None
    paired_ui_leds["receiver"] = None
    paired_observed_led_rows.clear()
    paired_observed_led_last_state.clear()
    receiver_panel_led_holder[0] = None
    for ch in list(paired_devices_inner.winfo_children()):
        try:
            ch.destroy()
        except tk.TclError:
            pass
    locs = read_all_locations_v2()
    cur_id = read_current_location_id()
    if not locs:
        tk.Label(
            paired_devices_inner,
            text="No locations yet — use Find device to add a Player and pick a room.",
            fg="#555",
            bg="#111",
            font=S_FONT_SMALL,
        ).pack(anchor=tk.W)
        return

    tk.Label(
        paired_devices_inner,
        text="Locations & saved devices",
        fg="#aaa",
        bg="#111",
        font=S_FONT_SEC,
    ).pack(anchor=tk.W, pady=(0, 4))

    # Per location: every saved role row is listed (same IP can appear as Player and TV, etc.).
    visible_locs: list[
        tuple[str, str, bool, dict[str, list[dict[str, str]]]]
    ] = []
    for loc in locs:
        loc_id = str(loc.get("id") or "")
        loc_name = str(loc.get("name") or "Room").strip()
        is_cur = bool(cur_id and loc_id == cur_id)
        slots: dict[str, list[dict[str, str]]] = {
            "streaming": _settings_parse_device_rows(loc.get("streaming")),
            "av_receiver": _settings_parse_receiver_rows(loc.get("av_receiver")),
            "tv": _settings_parse_device_rows(loc.get("tv")),
            "projector": _settings_parse_device_rows(loc.get("projector")),
            "game": _settings_parse_device_rows(loc.get("game")),
            "other": _settings_parse_device_rows(loc.get("other")),
        }
        if not any(slots.values()):
            continue
        visible_locs.append((loc_id, loc_name, is_cur, slots))

    if not visible_locs:
        tk.Label(
            paired_devices_inner,
            text="No devices saved in any location yet — use Find device.",
            fg="#555",
            bg="#111",
            font=S_FONT_SMALL,
        ).pack(anchor=tk.W)
        return

    loc_grid = tk.Frame(paired_devices_inner, bg="#111")
    loc_grid.pack(fill=tk.BOTH, expand=True)
    loc_grid.columnconfigure(0, weight=1, uniform="locpair")
    loc_grid.columnconfigure(1, weight=1, uniform="locpair")

    _aux_role_rows_ui: tuple[tuple[str, str, str, str, str], ...] = (
        ("tv", "TV", "#1a2520", "#3a5c4a", "#8fd4a8"),
        ("projector", "Projector", "#25201a", "#5c543a", "#d4c48f"),
        ("game", "Game", "#1a1f28", "#3a445c", "#9ab8ff"),
        ("other", "Other", "#222228", "#444454", "#c0c0d0"),
    )

    for idx, (loc_id, loc_name, is_cur, slots) in enumerate(visible_locs):
        stream_rows = slots["streaming"]
        av_rows = slots["av_receiver"]
        row, col = divmod(idx, 2)
        padx_g = (0, 5) if col == 0 else (5, 0)
        outer_bd = "#4a6a8a" if is_cur else "#3a3a44"
        outer = tk.Frame(
            loc_grid,
            bg="#15151c",
            highlightthickness=1,
            highlightbackground=outer_bd,
        )
        outer.grid(row=row, column=col, sticky="ew", padx=padx_g, pady=(0, 10))
        head_loc = tk.Frame(outer, bg="#15151c")
        head_loc.pack(fill=tk.X, padx=8, pady=(6, 4))
        sub = f"{loc_name}  (active)" if is_cur else loc_name
        tk.Label(
            head_loc,
            text=sub,
            fg="#8ec8ff" if is_cur else "#aaa",
            bg="#15151c",
            font=S_FONT_CAP_BOLD,
        ).pack(side=tk.LEFT)

        inner = tk.Frame(outer, bg="#15151c")
        inner.pack(fill=tk.X, padx=6, pady=(0, 6))

        for si, st in enumerate(stream_rows):
            nick = str(st.get("nickname") or "").strip()
            nm = nick or str(st.get("label") or st.get("name") or "Device").strip()
            ip = str(st.get("address") or "").strip()
            slot_tag = f" #{si + 1}" if len(stream_rows) > 1 else ""
            use_live = bool(is_cur and si == 0)
            if row_is_playback_apple_tv(st):
                grp = tk.Frame(
                    inner,
                    bg="#1a1a24",
                    highlightthickness=1,
                    highlightbackground="#333",
                )
                grp.pack(fill=tk.X, pady=(0, 6))

                def _player_row(
                    parent: tk.Frame,
                    led_key: str,
                    subtitle: str,
                    *,
                    use_live_leds: bool,
                    grp_bg: str,
                    ip_s: str,
                ) -> None:
                    line = tk.Frame(parent, bg=grp_bg)
                    line.pack(fill=tk.X, padx=8, pady=(2, 2))
                    cv = tk.Canvas(line, width=14, height=18, bg=grp_bg, highlightthickness=0, bd=0)
                    cv.pack(side=tk.LEFT)
                    # Never leave this blank while waiting for the first live poll.
                    _paint_cred_led_canvas(cv, False)
                    if use_live_leds:
                        paired_ui_leds[led_key] = cv
                    tk.Label(
                        line,
                        text=f"{subtitle}  ·  {ip_s}",
                        fg="#bbb",
                        bg=grp_bg,
                        font=S_FONT_SMALL,
                    ).pack(side=tk.LEFT, padx=(6, 0))

                head = tk.Frame(grp, bg="#1a1a24")
                head.pack(fill=tk.X, padx=8, pady=(6, 2))
                _paired_box_close_button(
                    head,
                    "#1a1a24",
                    lambda lid=loc_id, ix=si: _remove_streaming_device_at(lid, ix),
                ).pack(side=tk.RIGHT, padx=(8, 0))
                tk.Label(head, text="Player", fg="#7eb8ff", bg="#1a1a24", font=S_FONT_CAP_BOLD).pack(
                    side=tk.LEFT
                )
                tk.Label(head, text=f"{slot_tag}  {nm}", fg="#ccc", bg="#1a1a24", font=S_FONT_SMALL).pack(
                    side=tk.LEFT
                )
                _player_row(
                    grp,
                    "remote",
                    "AppleTV Remote",
                    use_live_leds=use_live,
                    grp_bg="#1a1a24",
                    ip_s=ip,
                )
                _player_row(
                    grp,
                    "airplay",
                    "AppleTV AirPlay",
                    use_live_leds=use_live,
                    grp_bg="#1a1a24",
                    ip_s=ip,
                )
            else:
                grp_o = tk.Frame(
                    inner,
                    bg="#1c1c20",
                    highlightthickness=1,
                    highlightbackground="#3a3a44",
                )
                grp_o.pack(fill=tk.X, pady=(0, 6))
                head_o = tk.Frame(grp_o, bg="#1c1c20")
                head_o.pack(fill=tk.X, padx=8, pady=(6, 6))
                _paired_box_close_button(
                    head_o,
                    "#1c1c20",
                    lambda lid=loc_id, ix=si: _remove_streaming_device_at(lid, ix),
                ).pack(side=tk.RIGHT, padx=(8, 0))
                tk.Label(head_o, text="Player", fg="#7eb8ff", bg="#1c1c20", font=S_FONT_CAP_BOLD).pack(
                    side=tk.LEFT
                )
                line_o = tk.Frame(grp_o, bg="#1c1c20")
                line_o.pack(fill=tk.X, padx=8, pady=(2, 6))
                cv_o = tk.Canvas(line_o, width=14, height=18, bg="#1c1c20", highlightthickness=0, bd=0)
                cv_o.pack(side=tk.LEFT)
                _paint_cred_led_canvas(cv_o, False)
                if is_cur:
                    paired_observed_led_rows.append((cv_o, str(loc_id), dict(st)))
                tk.Label(
                    line_o,
                    text=f"{slot_tag}  {nm}  ·  {ip}",
                    fg="#aaa",
                    bg="#1c1c20",
                    font=S_FONT_SMALL,
                ).pack(side=tk.LEFT, padx=(6, 0))
        for ai, av in enumerate(av_rows):
            grp_r = tk.Frame(
                inner,
                bg="#221a22",
                highlightthickness=1,
                highlightbackground="#5a3d5a",
            )
            grp_r.pack(fill=tk.X, pady=(0, 2))
            head_r = tk.Frame(grp_r, bg="#221a22")
            head_r.pack(fill=tk.X, padx=8, pady=(6, 2))
            _paired_box_close_button(
                head_r,
                "#221a22",
                lambda lid=loc_id, ix=ai: _remove_receiver_device_at(lid, ix),
            ).pack(side=tk.RIGHT, padx=(8, 0))
            tk.Label(head_r, text="Receiver", fg="#c9a0ff", bg="#221a22", font=S_FONT_CAP_BOLD).pack(
                side=tk.LEFT
            )
            rnick = str(av.get("nickname") or "").strip()
            an = rnick or str(av.get("label") or av.get("name") or "Receiver").strip()
            aip = str(av.get("address") or "").strip()
            rtag = f" #{ai + 1}" if len(av_rows) > 1 else ""
            line = tk.Frame(grp_r, bg="#221a22")
            line.pack(fill=tk.X, padx=8, pady=(2, 6))
            cv_r = tk.Canvas(line, width=14, height=18, bg="#221a22", highlightthickness=0, bd=0)
            cv_r.pack(side=tk.LEFT)
            if is_cur and ai == 0:
                paired_ui_leds["receiver"] = cv_r
                receiver_panel_led_holder[0] = cv_r
                _paint_boolean_led(cv_r, False)
            else:
                _paint_cred_led_canvas(cv_r, False)
            tk.Label(
                line,
                text=f"{rtag}  {an}  ·  {aip}",
                fg="#bbb",
                bg="#221a22",
                font=S_FONT_SMALL,
            ).pack(side=tk.LEFT, padx=(6, 0))

        for slot_key, role_title, bg_c, bd_c, title_c in _aux_role_rows_ui:
            aux_rows = slots.get(slot_key) or []
            for ri, arow in enumerate(aux_rows):
                nick_a = str(arow.get("nickname") or "").strip()
                nm_a = nick_a or str(arow.get("label") or arow.get("name") or role_title).strip()
                ip_a = str(arow.get("address") or "").strip()
                tag_a = f" #{ri + 1}" if len(aux_rows) > 1 else ""
                grp_a = tk.Frame(
                    inner,
                    bg=bg_c,
                    highlightthickness=1,
                    highlightbackground=bd_c,
                )
                grp_a.pack(fill=tk.X, pady=(0, 4))
                head_a = tk.Frame(grp_a, bg=bg_c)
                head_a.pack(fill=tk.X, padx=8, pady=(6, 6))
                _paired_box_close_button(
                    head_a,
                    bg_c,
                    lambda lid=loc_id, sk=slot_key, ix=ri, rt=role_title: _remove_aux_slot_device_at(
                        lid, sk, ix, role_title=rt
                    ),
                ).pack(side=tk.RIGHT, padx=(8, 0))
                tk.Label(
                    head_a,
                    text=role_title,
                    fg=title_c,
                    bg=bg_c,
                    font=S_FONT_CAP_BOLD,
                ).pack(side=tk.LEFT)
                line_a = tk.Frame(grp_a, bg=bg_c)
                line_a.pack(fill=tk.X, padx=8, pady=(2, 6))
                cv_a = tk.Canvas(line_a, width=14, height=18, bg=bg_c, highlightthickness=0, bd=0)
                cv_a.pack(side=tk.LEFT)
                _paint_cred_led_canvas(cv_a, False)
                if is_cur:
                    paired_observed_led_rows.append((cv_a, str(loc_id), dict(arow)))
                tk.Label(
                    line_a,
                    text=f"{tag_a}  {nm_a}  ·  {ip_a}",
                    fg="#ccc",
                    bg=bg_c,
                    font=S_FONT_SMALL,
                ).pack(side=tk.LEFT, padx=(6, 0))


def _on_delete_current_location(*, _apply_persisted_location_to_runtime, _refresh_location_selector, _start_location_toast, root) -> None:
    locs = read_all_locations_v2()
    if len(locs) <= 1:
        messagebox.showinfo(
            "Delete location",
            "You cannot delete the only location. Add another room first, then remove this one.",
            parent=root,
        )
        return
    cid = (read_current_location_id() or "").strip()
    if not cid:
        return
    nm = "this location"
    for L in locs:
        if str(L.get("id") or "").strip() == cid:
            nm = str(L.get("name") or "Room").strip() or "Room"
            break
    if not messagebox.askyesno(
        "Delete location",
        f'Delete "{nm}" and remove all saved devices and Advanced (delegation) data '
        f"for that room?\n\nThis cannot be undone.",
        parent=root,
    ):
        return
    try:
        from pigeon.observed_capability import clear_observed_capabilities_for_location

        if not delete_location_v2(cid):
            messagebox.showerror(
                "Delete location",
                "Could not delete that location.",
                parent=root,
            )
            return
        clear_observed_capabilities_for_location(cid)
    except Exception as e:
        messagebox.showerror("Delete location", str(e), parent=root)
        return
    _apply_persisted_location_to_runtime()
    _refresh_location_selector()
    _start_location_toast()
    messagebox.showinfo("Delete location", f'"{nm}" was deleted.')


def _ask_pairing_pin_modal(
    parent: tk.Misc,
    *,
    title: str,
    device_name: str,
    pair_kind: str,
    session_key: str | None,
    S_FONT_BODY, S_FONT_BTN, root,
) -> str | None:
    out: list[str | None] = [None]
    closed = [False]
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.configure(bg="#1a1a1e")
    try:
        dlg.transient(root)
        dlg.grab_set()
    except tk.TclError:
        pass
    tk.Label(
        dlg,
        text=f"{pair_kind}\nDevice: {device_name}\nEnter the 4-digit code shown on the television.",
        fg="#ddd",
        bg="#1a1a1e",
        font=S_FONT_BODY,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, padx=14, pady=(12, 8))
    pin_var = tk.StringVar(value="")

    def _close() -> None:
        if closed[0]:
            return
        closed[0] = True
        try:
            dlg.grab_release()
        except tk.TclError:
            pass
        dlg.destroy()

    ent = tk.Entry(
        dlg,
        textvariable=pin_var,
        width=12,
        font=("Menlo", 18) if sys.platform == "darwin" else ("Consolas", 18),
        justify=tk.CENTER,
        bg="#252528",
        fg="#e8e8e8",
        insertbackground="#e8e8e8",
    )
    ent.pack(padx=14, pady=(0, 8))

    def on_pin_write(*_args: object) -> None:
        raw = pin_var.get()
        d = "".join(c for c in raw if c.isdigit())[:4]
        if raw != d:
            pin_var.set(d)
            return
        if len(d) == 4 and not closed[0]:
            out[0] = d
            _close()

    pin_var.trace_add("write", on_pin_write)

    def append_digit(ch: str) -> None:
        cur = "".join(c for c in pin_var.get() if c.isdigit())
        if len(cur) < 4:
            pin_var.set(cur + ch)

    def backspace() -> None:
        cur = "".join(c for c in pin_var.get() if c.isdigit())
        pin_var.set(cur[:-1])

    pad = tk.Frame(dlg, bg="#1a1a1e")
    pad.pack(padx=10, pady=(0, 10))
    for keys in (("1", "2", "3"), ("4", "5", "6"), ("7", "8", "9")):
        rf = tk.Frame(pad, bg="#1a1a1e")
        rf.pack()
        for d in keys:
            tk.Button(
                rf,
                text=d,
                width=4,
                command=lambda x=d: append_digit(x),
                font=S_FONT_BTN,
            ).pack(side=tk.LEFT, padx=4, pady=4)
    rowz = tk.Frame(pad, bg="#1a1a1e")
    rowz.pack()
    tk.Button(rowz, text="\u232b", width=4, command=backspace, font=S_FONT_BTN).pack(
        side=tk.LEFT, padx=4, pady=4
    )
    tk.Button(rowz, text="0", width=4, command=lambda: append_digit("0"), font=S_FONT_BTN).pack(
        side=tk.LEFT, padx=4, pady=4
    )

    bf = tk.Frame(dlg, bg="#1a1a1e")
    bf.pack(pady=(0, 14))

    def on_ok() -> None:
        d = "".join(c for c in pin_var.get() if c.isdigit())
        if len(d) != 4:
            messagebox.showwarning("Pairing", "Enter the 4-digit code from the TV.", parent=dlg)
            return
        out[0] = d
        _close()

    def on_cancel() -> None:
        if session_key:
            try:
                from pigeon.apple_tv_now_playing import abandon_pairing_session

                abandon_pairing_session(session_key)
            except Exception:
                pass
        out[0] = None
        _close()

    tk.Button(bf, text="OK", command=on_ok, font=S_FONT_BTN, padx=12, pady=4).pack(
        side=tk.LEFT, padx=8
    )
    tk.Button(bf, text="Cancel", command=on_cancel, font=S_FONT_BTN, padx=12, pady=4).pack(
        side=tk.LEFT, padx=8
    )
    dlg.protocol("WM_DELETE_WINDOW", on_cancel)
    ent.focus_set()
    dlg.wait_window()
    return out[0]


def _remove_saved_player_device(for_location_id: str | None = None, *, _clear_reported_position_stall_stamp, _rebuild_paired_devices_panel, _schedule_refresh_pairing_leds, _sync_status_bar_visibility_for_playback, apple_tv_auto_state, apple_tv_busy, apple_tv_dashboard_track, apple_tv_playback_clock, current_apple_tv, describe_current_apple_tv, root, streaming_slot_holder) -> None:
    if apple_tv_busy["active"]:
        describe_current_apple_tv(suffix="busy")
        return
    if not messagebox.askyesno(
        "Remove Player",
        "Remove the saved Player device?\n\n"
        "Playback metadata stops using this Apple TV. "
        "pyatv credentials on this Mac are not deleted (use Reset to wipe those).",
        parent=root,
    ):
        return
    lid = (for_location_id or read_current_location_id() or "").strip()
    write_saved_streaming_device(None, for_location_id=lid or None)
    cur = read_current_location_id()
    if lid and cur and lid == cur:
        streaming_slot_holder[0] = None
        clear_last_apple_tv()
        current_apple_tv.clear()
        current_apple_tv.update(
            {"identifier": "", "address": "", "name": "", "label": ""}
        )
        apple_tv_auto_state["content_key"] = None
        apple_tv_auto_state["tmdb_key"] = None
        apple_tv_auto_state["query"] = None
        apple_tv_auto_state["last_metadata"] = None
        apple_tv_auto_state["last_tmdb_fetch_input"] = None
        apple_tv_auto_state["last_tmdb_fetch_refined"] = None
        apple_tv_auto_state["last_tmdb_fetch_prefer"] = None
        apple_tv_playback_clock.clear()
        apple_tv_playback_clock.update(
            {
                "has_sync": False,
                "sync_mono": 0.0,
                "sync_position": 0.0,
                "live_mode": False,
                "playing": False,
                "latched_total": None,
                "latched_content_key": None,
                "last_reported_total": None,
                "display_played_sec": None,
                "trt_next_fire_mono": None,
            }
        )
        _clear_reported_position_stall_stamp()
        apple_tv_dashboard_track["last_poll_ok"] = None
        apple_tv_dashboard_track["consecutive_fail"] = 0
        _sync_status_bar_visibility_for_playback(None)
    else:
        streaming_slot_holder[0] = read_saved_streaming_device()
    describe_current_apple_tv()
    _rebuild_paired_devices_panel()
    _schedule_refresh_pairing_leds()


def _settings_is_native_1280(*, DevPhase, dev_phase, main_settings_widget) -> bool:
    """True when settings is on screen — all current pages are 1280×800."""
    return dev_phase[0] == DevPhase.MAIN_SETTINGS and main_settings_widget is not None


def _settings_menu_is_static(*, DevPhase, dev_phase, main_settings_widget) -> bool:
    """True when settings is up and not running a scan/spinner animation."""
    if dev_phase[0] != DevPhase.MAIN_SETTINGS or main_settings_widget is None:
        return False
    try:
        st = main_settings_widget.state
    except Exception:
        return False
    return not (
        st.wifi_scanning
        or st.wifi_connecting
        or st.box2_devices.scanning
        or st.box3_devices.scanning
        or st.location_switching
    )


def _sync_settings_zone2_tt(*, main_settings_widget) -> None:
    """Box1 stays pigeon wordmark + IP; drop any leftover TT payload."""
    if main_settings_widget is None:
        return
    st_ms = main_settings_widget.state
    if getattr(st_ms, "zone2_tt_bgra", None) is not None:
        st_ms.zone2_tt_bgra = None


def _start_location_toast(*, startup: bool = False, _PIGEON_EXT, _current_location_display_name, location_toast_state, skip_cache) -> None:
    if not _PIGEON_EXT:
        return
    st = location_toast_state
    st["text"] = _current_location_display_name()
    st["active"] = True
    st["t0"] = time.monotonic()
    st["startup_top_left"] = bool(startup)
    st["hold_full_s"] = 15.0 if startup else 5.0
    skip_cache[0] = None


def _paint_coalesced_settings_nav(*, _nav_coalescer_holder, main_settings_widget, render_once, skip_cache) -> None:
    skip_cache[0] = None
    coalescer = _nav_coalescer_holder[0]
    if main_settings_widget is not None and (
        coalescer is None or not coalescer.is_hot()
    ):
        main_settings_widget._nav_scrub = False
    render_once()


def _request_settings_nav_paint(*, _nav_coalescer_holder, main_settings_widget, render_once, skip_cache) -> None:
    skip_cache[0] = None
    if main_settings_widget is not None:
        main_settings_widget._nav_scrub = True
    coalescer = _nav_coalescer_holder[0]
    if coalescer is None:
        render_once()
        return
    coalescer.request()


def _remove_saved_receiver_device(for_location_id: str | None = None, *, _rebuild_paired_devices_panel, _schedule_refresh_pairing_leds, _warm_playback_overlay_blits, apple_tv_busy, avr_slot_holder, describe_current_apple_tv, playback_overlay_widget, receiver_http_host, render_once, root, skip_cache) -> None:
    if apple_tv_busy["active"]:
        describe_current_apple_tv(suffix="busy")
        return
    if not messagebox.askyesno(
        "Remove Receiver",
        "Remove the saved Receiver device and stop the overlay status poll for it?",
        parent=root,
    ):
        return
    lid = (for_location_id or read_current_location_id() or "").strip()
    write_saved_av_receiver(None, for_location_id=lid or None)
    cur = read_current_location_id()
    if lid and cur and lid == cur:
        avr_slot_holder[0] = None
        clear_last_receiver()
        receiver_http_host["host"] = ""
        if playback_overlay_widget is not None:
            playback_overlay_widget.clear_cache()
        try:
            _warm_playback_overlay_blits()
        except Exception:
            pass
        skip_cache[0] = None
        try:
            render_once()
        except Exception:
            pass
    else:
        avr_slot_holder[0] = read_saved_av_receiver()
    describe_current_apple_tv()
    _rebuild_paired_devices_panel()
    _schedule_refresh_pairing_leds()


def _check_for_updates(*, force: bool = False, _UPDATE_CHECK_INTERVAL_S, _finish_update_check, root, update_check_state) -> None:
    if update_check_state.get("checking"):
        return
    now = time.monotonic()
    last = float(update_check_state.get("last_check_mono") or 0.0)
    if not force and (now - last) < _UPDATE_CHECK_INTERVAL_S:
        return
    update_check_state["checking"] = True

    def worker() -> None:
        try:
            from pigeon.update_check import check_for_update

            result = check_for_update()
        except Exception as e:
            from pigeon.update_check import UpdateCheckResult

            result = UpdateCheckResult(
                local_version=version_string(),
                remote_version=None,
                update_available=False,
                error=str(e),
            )

        root.after(0, lambda r=result: _finish_update_check(r))

    threading.Thread(target=worker, daemon=True).start()


def _handle_main_settings_action(action: str, *, _apply_persisted_location_to_runtime, _pyatv_install_hint, _rebuild_paired_devices_panel, _refresh_location_selector, _resolve_install_root_for_update, _save_box_pair_device_row, _schedule_refresh_pairing_leds, _sync_update_button_style, apple_tv_auto_state, apple_tv_dashboard_track, avr_slot_holder, begin_apple_tv_operation, current_apple_tv, describe_current_apple_tv, discovery_scan_cache, end_apple_tv_operation, main_settings_widget, pair_led_busy, receiver_http_host, root, skip_cache, streaming_slot_holder, update_check_state) -> None:
    if main_settings_widget is None:
        return
    st = main_settings_widget.state

    if action == "pigeon_settings":
        # Entered settings_pigeon — seed update badge and refresh in background.
        def _prefetch_pigeon_update_badge() -> None:
            if not getattr(st, "pigeon_needs_update_prefetch", False):
                return
            st.pigeon_needs_update_prefetch = False
            try:
                if update_check_state.get("update_available") and not update_check_state.get(
                    "error"
                ):
                    st.update_available = True
                    st.update_remote_version = update_check_state.get("remote_version")
                    st.update_github_branch = update_check_state.get("github_branch")
                    st.update_error = None
                    main_settings_widget.invalidate()
                    skip_cache[0] = None
            except Exception:
                pass

            def worker_prefetch() -> None:
                try:
                    from pigeon.update_check import check_for_update

                    result = check_for_update(force=False)
                except Exception as e:
                    from pigeon.update_check import UpdateCheckResult

                    result = UpdateCheckResult(
                        local_version=version_string(),
                        remote_version=None,
                        update_available=False,
                        error=str(e),
                    )

                def finish_prefetch() -> None:
                    if main_settings_widget is None:
                        return
                    st2 = main_settings_widget.state
                    if not st2.show_pigeon_settings or st2.show_update_popup:
                        return
                    err = getattr(result, "error", None)
                    available = bool(getattr(result, "update_available", False)) and not err
                    st2.update_available = available
                    st2.update_remote_version = getattr(result, "remote_version", None)
                    st2.update_github_branch = getattr(result, "github_branch", None)
                    st2.update_error = err
                    try:
                        update_check_state["update_available"] = bool(available)
                        update_check_state["remote_version"] = st2.update_remote_version
                        update_check_state["github_branch"] = st2.update_github_branch
                        update_check_state["error"] = err
                        update_check_state["last_check_mono"] = time.monotonic()
                        _sync_update_button_style()
                    except Exception:
                        pass
                    main_settings_widget.invalidate()
                    skip_cache[0] = None

                root.after(0, finish_prefetch)

            threading.Thread(
                target=worker_prefetch, name="pigeon-update-prefetch", daemon=True
            ).start()

        _prefetch_pigeon_update_badge()
        return

    if action == "pigeon_factory_reset":
        from pigeon.widgets.pigeon_settings import factory_reset_pigeon_persisted_state
        from pigeon.widgets.ui_color_settings import (
            apply_color_keys_to_state,
            load_persisted_theme_into_state,
        )

        factory_reset_pigeon_persisted_state()
        try:
            discovery_scan_cache["rows"] = None
            discovery_scan_cache["mono_s"] = 0.0
        except Exception:
            pass
        try:
            streaming_slot_holder[0] = None
            avr_slot_holder[0] = None
        except Exception:
            pass
        try:
            current_apple_tv.clear()
            current_apple_tv.update(
                {"identifier": "", "address": "", "name": "", "label": ""}
            )
        except Exception:
            pass
        try:
            receiver_http_host["host"] = ""
        except Exception:
            pass
        try:
            apple_tv_auto_state["content_key"] = None
            apple_tv_auto_state["tmdb_key"] = None
            apple_tv_auto_state["query"] = None
            apple_tv_auto_state["last_metadata"] = None
            apple_tv_dashboard_track["last_poll_ok"] = None
            apple_tv_dashboard_track["consecutive_fail"] = 0
        except Exception:
            pass
        try:
            load_persisted_theme_into_state(st)
            apply_color_keys_to_state(
                st,
                {"accent": "white", "ui": "blue", "button": "black"},
                persist=False,
            )
        except Exception:
            pass
        try:
            st.reset_box_device_panel(2)
            st.reset_box_device_panel(3)
        except Exception:
            pass
        st.selected_wifi_ssid = ""
        st.live_wifi_ssid = ""
        st.wifi_logged_out = False
        st.pigeon_metadata_ok = False
        st.pigeon_hdmi_ok = False
        st.pigeon_audio_ok = False
        try:
            from pigeon.source_status import apply_source_status_to_settings_state

            apply_source_status_to_settings_state(st)
        except Exception:
            pass
        try:
            describe_current_apple_tv()
            _refresh_location_selector()
            _rebuild_paired_devices_panel()
            _schedule_refresh_pairing_leds()
        except Exception:
            pass
        main_settings_widget.invalidate()
        skip_cache[0] = None
        return

    if action == "update_popup:open":
        # Always re-check GitHub (ignore any prior in-memory poll).
        st.update_local_version = version_string()
        st.update_checking = True
        st.update_error = None
        st.update_available = False
        st.update_remote_version = None
        st.update_github_branch = None
        st.update_changelog = "Checking GitHub for updates…"
        main_settings_widget.invalidate()
        skip_cache[0] = None

        def worker_ms_update_check() -> None:
            try:
                from pigeon.update_check import check_for_update

                result = check_for_update(force=True)
            except Exception as e:
                from pigeon.update_check import UpdateCheckResult

                result = UpdateCheckResult(
                    local_version=version_string(),
                    remote_version=None,
                    update_available=False,
                    error=str(e),
                )

            def finish_ms_check() -> None:
                from pigeon.widgets.update_popup import (
                    DEFAULT_CHANGELOG,
                    UP_TO_DATE_CHANGELOG,
                )

                st.update_checking = False
                st.update_local_version = str(
                    getattr(result, "local_version", None) or version_string()
                )
                st.update_remote_version = getattr(result, "remote_version", None)
                st.update_github_branch = getattr(result, "github_branch", None)
                st.update_error = getattr(result, "error", None)
                available = bool(getattr(result, "update_available", False))
                st.update_available = available and not st.update_error
                if st.update_available:
                    st.update_changelog = DEFAULT_CHANGELOG
                    try:
                        from pigeon.widgets.update_popup import update_popup_focus_ring

                        ring = update_popup_focus_ring(update_available=True)
                        st.update_popup_focus_index = (
                            ring.index("now") if "now" in ring else 0
                        )
                    except Exception:
                        st.update_popup_focus_index = 1
                else:
                    st.update_changelog = UP_TO_DATE_CHANGELOG
                    st.update_popup_focus_index = 0
                # Keep legacy Tk Updates button in sync when present.
                try:
                    update_check_state["update_available"] = bool(st.update_available)
                    update_check_state["remote_version"] = st.update_remote_version
                    update_check_state["github_branch"] = st.update_github_branch
                    update_check_state["error"] = st.update_error
                    update_check_state["last_check_mono"] = time.monotonic()
                    _sync_update_button_style()
                except Exception:
                    pass
                main_settings_widget.invalidate()
                skip_cache[0] = None

            root.after(0, finish_ms_check)

        threading.Thread(target=worker_ms_update_check, daemon=True).start()
        return

    if action in ("update_popup:later", "update_popup:dismiss", "update_popup:busy"):
        main_settings_widget.invalidate()
        skip_cache[0] = None
        return

    if action == "update_popup:now":
        if st.update_applying or st.update_checking:
            return
        if not st.update_available:
            st.close_update_popup()
            main_settings_widget.invalidate()
            skip_cache[0] = None
            return
        remote = str(st.update_remote_version or "?")
        branch = st.update_github_branch
        st.update_applying = True
        st.update_progress = 0.0
        st.update_changelog = f"Downloading {remote}…"
        main_settings_widget.invalidate()
        skip_cache[0] = None
        # Worker never calls root.after — Tk is not thread-safe and flooding
        # after() from download progress can drop the final restart callback.
        _ms_events: queue.Queue = queue.Queue()
        _ms_progress_last = [0.0]
        _ms_progress_label = [""]
        _ms_poll_active = [True]

        def _ms_on_progress(fraction: float, label: str) -> None:
            frac = max(0.0, min(1.0, float(fraction)))
            lab = str(label or "Updating…")[:96]
            if (
                frac < 0.999
                and frac - _ms_progress_last[0] < 0.02
                and lab == _ms_progress_label[0]
            ):
                return
            _ms_progress_last[0] = frac
            _ms_progress_label[0] = lab
            _ms_events.put(("progress", frac, lab))

        def _ms_poll_events() -> None:
            if main_settings_widget is None:
                return
            done_payload = None
            try:
                while True:
                    kind, *payload = _ms_events.get_nowait()
                    if kind == "progress":
                        frac, lab = payload
                        if st.update_applying:
                            st.update_progress = float(frac)
                            st.update_changelog = str(lab)
                            main_settings_widget.invalidate()
                            skip_cache[0] = None
                    elif kind == "done":
                        done_payload = payload[0]
            except queue.Empty:
                pass
            if done_payload is not None:
                _ms_poll_active[0] = False
                result, install_root = done_payload
                if result.ok:
                    st.update_progress = 1.0
                    st.update_changelog = "Update complete — restarting…"
                    st.update_available = False
                    try:
                        update_check_state["update_available"] = False
                        if result.remote_version:
                            update_check_state["remote_version"] = (
                                result.remote_version
                            )
                        _sync_update_button_style()
                    except Exception:
                        pass
                    main_settings_widget.invalidate()
                    skip_cache[0] = None

                    def _restart_ms() -> None:
                        try:
                            # Linux curl|bash updater already schedules
                            # in-app relaunch (PIGEON_UPDATE_IN_APP=1). On
                            # macOS/desktop we must schedule it here.
                            if not sys.platform.startswith("linux"):
                                from pigeon.github_update import (
                                    restart_pigeon_after_update,
                                )

                                restart_pigeon_after_update(
                                    install_root, parent_pid=os.getpid()
                                )
                        except Exception:
                            pass
                        # Exit unconditionally — do not wait on root.destroy().
                        os._exit(0)

                    root.after(400, _restart_ms)
                    return

                st.update_applying = False
                st.update_progress = 0.0
                st.update_error = result.message
                st.update_changelog = (result.message or "Update failed.")[:96]
                main_settings_widget.invalidate()
                skip_cache[0] = None
                messagebox.showerror(
                    "Update failed",
                    result.message,
                    parent=root,
                )
                return
            if _ms_poll_active[0]:
                root.after(50, _ms_poll_events)

        def worker_ms_apply() -> None:
            install_root = _resolve_install_root_for_update()
            try:
                from pigeon.github_update import apply_github_update

                apply_branch = branch
                if apply_branch is None:
                    cached = update_check_state.get("github_branch")
                    if isinstance(cached, str) and cached.strip():
                        apply_branch = cached.strip()
                result = apply_github_update(
                    install_root,
                    branch=apply_branch,
                    progress=_ms_on_progress,
                )
            except Exception as e:
                from pigeon.github_update import ApplyUpdateResult

                result = ApplyUpdateResult(False, str(e))
            _ms_events.put(("done", (result, install_root)))

        root.after(50, _ms_poll_events)
        threading.Thread(target=worker_ms_apply, daemon=True).start()
        return

    if action == "keyboard_pin_incomplete":
        messagebox.showwarning("Pairing", "Enter the 4-digit code from the TV.", parent=root)
        return

    if action == "wifi_logout:yes":
        try:
            from pigeon.app_state import clear_location_wifi

            clear_location_wifi()
        except Exception:
            pass
        st.selected_wifi_ssid = ""
        st.live_wifi_ssid = ""
        st.wifi_logged_out = True
        st.wifi_password = ""
        st.pending_wifi_ssid = ""
        st.pending_network_password = ""
        st.network_password_error = False
        st.ensure_focus_ring()
        main_settings_widget.invalidate()
        skip_cache[0] = None
        return

    if action == "wifi_logout:no":
        main_settings_widget.invalidate()
        skip_cache[0] = None
        return

    if action == "keyboard_go:network":
        ssid = str(st.pending_wifi_ssid or "").strip()
        password = str(st.pending_network_password or "")
        if not ssid:
            return

        st.wifi_connecting = True
        st.wifi_connect_started_mono = time.monotonic()
        st.wifi_scan_angle_deg = 0.0
        st.network_password_error = False
        main_settings_widget.invalidate()
        skip_cache[0] = None

        def worker_wifi_join() -> None:
            try:
                from pigeon.wifi_connect import try_join_wifi_network

                ok_w, _msg_w = try_join_wifi_network(ssid, password)
            except Exception:
                ok_w, _msg_w = False, "incorrect password"

            def finish_wifi() -> None:
                st.wifi_connecting = False
                if ok_w:
                    st.wifi_logged_out = False
                    st.selected_wifi_ssid = ssid
                    st.live_wifi_ssid = ssid
                    st.wifi_password = password
                    st.pending_wifi_ssid = ""
                    st.pending_network_password = ""
                    st.network_password_error = False
                    st.wifi_onboarding = False
                    st.show_instructions = False
                    st.ensure_focus_ring()
                    try:
                        write_location_wifi(ssid, password)
                    except Exception:
                        pass
                    try:
                        from pigeon.local_ip import clear_local_ipv4_cache

                        clear_local_ipv4_cache()
                    except Exception:
                        pass
                else:
                    st.network_password_error = True
                    st.pending_network_password = ""
                    st.open_keyboard(
                        "network",
                        assets_dir=main_settings_widget._assets_dir,
                        trigger_button="main_dual_network_button",
                    )
                main_settings_widget.invalidate()
                skip_cache[0] = None

            root.after(0, finish_wifi)

        threading.Thread(target=worker_wifi_join, daemon=True).start()
        return

    if action == "keyboard_go:device_name":
        avr = read_saved_av_receiver()
        if avr:
            avr_slot_holder[0] = avr
            adr = str(avr.get("address") or "").strip()
            if adr:
                receiver_http_host["host"] = adr
        picked = st.box3_devices.picked
        if picked and str(picked[1] or "").strip():
            receiver_http_host["host"] = str(picked[1]).strip()
        main_settings_widget.invalidate()
        skip_cache[0] = None
        return

    if action == "keyboard_go:location":
        nm = str(st.location_name or "").strip() or "Room"
        lid = str(getattr(st, "renaming_location_id", "") or "").strip()
        if not lid:
            lid = read_current_location_id()
        if lid:
            rename_location_v2(lid, nm)
        try:
            st.refresh_location_slots()
        except Exception:
            pass
        st.renaming_location_id = ""
        st.renaming_location_slot = 0
        try:
            st.location_name = read_current_location_name()
        except Exception:
            st.location_name = nm
        if st.show_location_picker:
            st.ensure_focus_ring()
        main_settings_widget.invalidate()
        skip_cache[0] = None
        return

    if action == "location_switch:busy":
        return

    if action == "location_switch":
        if st.location_switching:
            return
        st.begin_location_switching()
        try:
            main_settings_widget._ensure_location_switch_spinner_frames()
        except Exception:
            pass
        main_settings_widget.invalidate()
        skip_cache[0] = None

        def _finish_location_switch() -> None:
            if main_settings_widget is None:
                return
            st_fin = main_settings_widget.state
            # Keep spinner up until pairing LED credential check settles.
            if pair_led_busy.get("active"):
                root.after(100, _finish_location_switch)
                return
            st_fin.finish_location_switching()
            main_settings_widget.invalidate()
            skip_cache[0] = None

        def _run_location_switch() -> None:
            try:
                _apply_persisted_location_to_runtime()
                try:
                    st.load_saved_box_devices()
                    st.location_name = read_current_location_name()
                except Exception:
                    pass
                try:
                    st.reload_location_wifi()
                except Exception:
                    pass
                try:
                    st.refresh_location_slots()
                except Exception:
                    pass
            finally:
                main_settings_widget.invalidate()
                skip_cache[0] = None
                root.after(0, _finish_location_switch)

        # Yield so the UI can paint; spinner appears if reload exceeds ~2s.
        root.after(0, _run_location_switch)
        return

    if action == "box3_pair_start":
        sess = st.box_pairing
        if sess is None or int(sess.box_num) != 3:
            return
        _save_box_pair_device_row(3, sess.row)
        name = str(sess.row.get("name") or sess.row.get("label") or "Receiver").strip()
        ip = str(sess.row.get("address") or "").strip()
        if ip:
            st.box3_devices.picked = (name or ip, ip)
            st.box3_ip_invalid = False
        st.show_box3_panel = True
        st.clear_box_pairing()
        main_settings_widget.invalidate()
        skip_cache[0] = None
        _schedule_refresh_pairing_leds()
        _rebuild_paired_devices_panel()
        return

    if action == "box2_pair_start":
        sess = st.box_pairing
        if sess is None or int(sess.box_num) != 2:
            return
        row = dict(sess.row)
        if not begin_apple_tv_operation("starting AppleTV Remote"):
            return

        def worker_remote_begin_settings() -> None:
            try:
                from pigeon.apple_tv_now_playing import begin_companion_pairing_for_device

                ok_w, msg_w, session_key_w, _rev = begin_companion_pairing_for_device(
                    device_identifier=row["identifier"],
                    device_address=row["address"],
                    tv_displays_pin=True,
                )
            except ImportError:
                ok_w, msg_w, session_key_w = False, _pyatv_install_hint(), None
            except Exception as e:
                ok_w, msg_w, session_key_w = False, str(e), None

            def finish_rb_settings() -> None:
                if not ok_w or not session_key_w:
                    end_apple_tv_operation()
                    st.clear_box_pairing()
                    main_settings_widget.invalidate()
                    skip_cache[0] = None
                    messagebox.showerror("AppleTV Remote", msg_w or "Pairing failed to start.")
                    return
                if st.box_pairing is not None:
                    st.box_pairing.session_key = str(session_key_w)
                    st.box_pairing.step = "remote_pin"
                st.open_keyboard("pin", assets_dir=main_settings_widget._assets_dir)
                main_settings_widget.invalidate()
                skip_cache[0] = None
                describe_current_apple_tv(suffix="enter Remote PIN")

            root.after(0, finish_rb_settings)

        threading.Thread(target=worker_remote_begin_settings, daemon=True).start()
        return

    if action.startswith("keyboard_pin:"):
        pin = action.split(":", 1)[1].strip()
        pin = "".join(c for c in pin if c.isdigit())
        if len(pin) != 4:
            messagebox.showwarning("Pairing", "Enter the 4-digit code from the TV.", parent=root)
            return
        sess = st.box_pairing
        if sess is None or not sess.session_key:
            return
        row = dict(sess.row)
        dn = sess.device_name

        if sess.step == "remote_pin":

            def worker_remote_finish_settings() -> None:
                try:
                    from pigeon.apple_tv_now_playing import begin_airplay_pairing_for_device, finish_companion_pairing_for_device

                    ok_f, msg_f = finish_companion_pairing_for_device(
                        session_key=sess.session_key, pin_code=pin
                    )
                except ImportError:
                    ok_f, msg_f = False, _pyatv_install_hint()
                except Exception as e:
                    ok_f, msg_f = False, str(e)

                if ok_f:
                    time.sleep(1.5)

                def done_rf_settings() -> None:
                    if not ok_f:
                        end_apple_tv_operation()
                        st.clear_box_pairing()
                        main_settings_widget.invalidate()
                        skip_cache[0] = None
                        messagebox.showerror("AppleTV Remote", msg_f)
                        _schedule_refresh_pairing_leds()
                        return
                    try:
                        ok_a, msg_a, sk_a, _r2 = begin_airplay_pairing_for_device(
                            device_identifier=row["identifier"],
                            device_address=row["address"],
                            tv_displays_pin=True,
                        )
                    except ImportError:
                        ok_a, msg_a, sk_a = False, _pyatv_install_hint(), None
                    except Exception as e:
                        ok_a, msg_a, sk_a = False, str(e), None
                    if not ok_a or not sk_a:
                        end_apple_tv_operation()
                        st.clear_box_pairing()
                        main_settings_widget.invalidate()
                        skip_cache[0] = None
                        messagebox.showerror("AppleTV AirPlay", msg_a or "AirPlay pairing failed to start.")
                        _schedule_refresh_pairing_leds()
                        return
                    if st.box_pairing is not None:
                        st.box_pairing.session_key = str(sk_a)
                        st.box_pairing.step = "airplay_pin"
                    st.open_keyboard("pin", assets_dir=main_settings_widget._assets_dir)
                    main_settings_widget.invalidate()
                    skip_cache[0] = None
                    describe_current_apple_tv(suffix="enter AirPlay PIN")

                root.after(0, done_rf_settings)

            threading.Thread(target=worker_remote_finish_settings, daemon=True).start()
            return

        if sess.step == "airplay_pin":

            def worker_air_finish_settings() -> None:
                try:
                    from pigeon.apple_tv_now_playing import finish_companion_pairing_for_device

                    ok_af, msg_af = finish_companion_pairing_for_device(
                        session_key=sess.session_key, pin_code=pin
                    )
                except ImportError:
                    ok_af, msg_af = False, _pyatv_install_hint()
                except Exception as e:
                    ok_af, msg_af = False, str(e)

                def done_air_settings() -> None:
                    end_apple_tv_operation()
                    if ok_af:
                        _save_box_pair_device_row(2, row)
                        st.load_saved_box_devices()
                        st.show_box2_panel = True
                    st.clear_box_pairing()
                    main_settings_widget.invalidate()
                    skip_cache[0] = None
                    if ok_af:
                        try:
                            sys.stderr.write(
                                f"pigeon: Apple TV AirPlay paired for Player {dn!r}: {msg_af}\n"
                            )
                            sys.stderr.flush()
                        except Exception:
                            pass
                    else:
                        messagebox.showerror("AppleTV AirPlay", msg_af)
                    _schedule_refresh_pairing_leds()
                    _rebuild_paired_devices_panel()

                root.after(0, done_air_settings)

            threading.Thread(target=worker_air_finish_settings, daemon=True).start()


def _apply_location_rename(*, _apply_persisted_location_to_runtime, _refresh_location_selector, _start_location_toast, location_name_var) -> None:
    cid = (read_current_location_id() or "").strip()
    if not cid:
        return
    new_nm = (location_name_var.get() or "").strip() or "Room"
    if not rename_location_v2(cid, new_nm):
        return
    _refresh_location_selector()
    _apply_persisted_location_to_runtime()
    _start_location_toast()


def _schedule_periodic_update_check(*, _UPDATE_CHECK_INTERVAL_S, _check_for_updates, _schedule_periodic_update_check, root) -> None:
    _check_for_updates(force=False)
    root.after(int(_UPDATE_CHECK_INTERVAL_S * 1000), _schedule_periodic_update_check)


def _composite_settings_on_canvas(canvas: np.ndarray, *, _nav_coalescer_holder, _show_paused_row_overlay, _something_playing_now, _sync_now_playing_screen_state, _sync_preferences_now_playing_progress, _sync_settings_zone2_tt, main_settings_widget, view_circles_widget) -> None:
    """Paint settings, then the NP status bar on settings_main while content is up."""
    if main_settings_widget is None:
        return
    coalescer = _nav_coalescer_holder[0]
    nav_hot = bool(coalescer is not None and coalescer.is_hot())
    if not nav_hot:
        try:
            if not bool(getattr(main_settings_widget.state, "show_widgets", False)):
                _sync_preferences_now_playing_progress()
                _sync_settings_zone2_tt()
        except Exception:
            pass
    main_settings_widget.render(canvas)
    try:
        st_ms = main_settings_widget.state
        pigeon_page = bool(st_ms.show_pigeon_settings)
    except Exception:
        return
    playing = False
    try:
        playing = bool(_something_playing_now() or _show_paused_row_overlay())
    except Exception:
        playing = False
    from pigeon.widgets.view_circles import settings_main_keeps_np_status_bar

    if not settings_main_keeps_np_status_bar(
        show_pigeon_settings=pigeon_page,
        content_playing=playing,
    ):
        return
    if view_circles_widget is None:
        return
    # Keep the bar on every nav paint. Skipping it while the coalescer
    # is hot made the track vanish and pop back on each Left/Right.
    if not nav_hot:
        try:
            _sync_now_playing_screen_state()
        except Exception:
            pass
    try:
        view_circles_widget.overlay_status_bar(canvas)
    except Exception:
        pass


def _refresh_location_selector(*, _apply_persisted_location_to_runtime, _refresh_location_selector, delete_location_btn, location_menu_var, location_name_entry, location_name_var, location_om_frame, location_option_holder, rename_name_btn, root) -> None:
    for w in location_om_frame.winfo_children():
        try:
            w.destroy()
        except tk.TclError:
            pass
    locs = read_all_locations_v2()
    labels: list[str] = []
    ids: list[str] = []
    counts: dict[str, int] = {}
    for L in locs:
        base = str(L.get("name") or "Room").strip() or "Room"
        counts[base] = counts.get(base, 0) + 1
        c = counts[base]
        lab = base if c == 1 else f"{base} ({c})"
        labels.append(lab)
        ids.append(str(L.get("id") or ""))
    labels.append("+ Custom location…")
    ids.append("__custom__")
    cur_id = read_current_location_id()

    def _pick(label_val: str) -> None:
        if label_val == "+ Custom location…":
            name = simpledialog.askstring(
                "Custom location",
                "Name for this location:",
                parent=root,
            )
            if name and str(name).strip():
                add_empty_location_v2(str(name).strip())
                _apply_persisted_location_to_runtime()
            _refresh_location_selector()
            return
        idx = labels.index(label_val) if label_val in labels else -1
        if idx < 0 or idx >= len(ids):
            return
        lid = ids[idx]
        if lid == "__custom__":
            return
        if lid == read_current_location_id():
            return
        if lid and set_current_location_id(lid):
            _apply_persisted_location_to_runtime()
            _refresh_location_selector()

    if not locs:
        location_menu_var.set("+ Custom location…")

        def _pick_empty(val: str) -> None:
            if val == "+ Custom location…":
                name = simpledialog.askstring(
                    "Custom location",
                    "Name for this location:",
                    parent=root,
                )
                if name and str(name).strip():
                    add_empty_location_v2(str(name).strip())
                    _apply_persisted_location_to_runtime()
                _refresh_location_selector()

        om = tk.OptionMenu(
            location_om_frame,
            location_menu_var,
            "+ Custom location…",
            command=_pick_empty,
        )
        om.pack(side=tk.LEFT)
        location_option_holder[0] = om
        location_name_var.set("")
        try:
            location_name_entry.configure(state=tk.DISABLED)
            rename_name_btn.configure(state=tk.DISABLED)
            delete_location_btn.configure(state=tk.DISABLED)
        except tk.TclError:
            pass
        return

    cur_label = labels[0]
    if cur_id:
        for i, xid in enumerate(ids):
            if xid == cur_id and i < len(labels) - 1:
                cur_label = labels[i]
                break
    location_menu_var.set(cur_label)
    om = tk.OptionMenu(location_om_frame, location_menu_var, *labels, command=_pick)
    om.pack(side=tk.LEFT)
    location_option_holder[0] = om
    cid_nm = (read_current_location_id() or "").strip()
    raw_nm = ""
    if cid_nm:
        for L in read_all_locations_v2():
            if str(L.get("id") or "").strip() == cid_nm:
                raw_nm = str(L.get("name") or "Room").strip() or "Room"
                break
    location_name_var.set(raw_nm)
    try:
        location_name_entry.configure(state=tk.NORMAL if cid_nm else tk.DISABLED)
        rename_name_btn.configure(state=tk.NORMAL if cid_nm else tk.DISABLED)
        delete_location_btn.configure(
            state=tk.NORMAL if len(locs) > 1 and cid_nm else tk.DISABLED
        )
    except tk.TclError:
        pass


def _force_advanced_feature_try(feature_id: str, *, _apple_tv_auto_poll_tick, _receiver_poll_tick, on_apple_tv_selected_then_tmdb) -> None:
    """Advanced matrix refresh: re-run the probe path relevant to this feature row."""
    try:
        if feature_id == "title":
            on_apple_tv_selected_then_tmdb()
        else:
            if feature_id == "volume":
                _receiver_poll_tick()
            _apple_tv_auto_poll_tick()
    except Exception:
        pass


def on_reset_pigeon_devices_and_media(*, _rebuild_paired_devices_panel, _refresh_location_selector, _schedule_refresh_pairing_leds, _warm_playback_overlay_blits, apple_tv_auto_state, apple_tv_dashboard_track, avr_slot_holder, current_apple_tv, describe_current_apple_tv, discovery_scan_cache, playback_overlay_widget, receiver_http_host, render_once, root, skip_cache, streaming_slot_holder) -> None:
    if not messagebox.askokcancel(
        "Reset",
        "This wipes everything Pigeon has stored for devices and local image media:\n\n"
        "• Saved locations, Players, and Receivers\n"
        "• pyatv credentials\n"
        "• Discovery cache\n"
        "• pigeonPulledMedia and pigeonReformattedMedia\n\n"
        "This cannot be undone. Continue?",
        parent=root,
    ):
        return
    ok1, msg1 = purge_directory_contents(pigeon_pulled_media_dir())
    ok2, msg2 = purge_directory_contents(pigeon_reformatted_media_dir())
    cred_path = pigeon_state_dir() / "pyatv_credentials"
    cred_err = ""
    try:
        if cred_path.is_file():
            cred_path.unlink()
    except OSError as e:
        cred_err = str(e)
    clear_all_persisted_devices_and_targets()
    clear_last_apple_tv()
    clear_last_receiver()
    discovery_scan_cache["rows"] = None
    discovery_scan_cache["mono_s"] = 0.0
    streaming_slot_holder[0] = None
    avr_slot_holder[0] = None
    current_apple_tv.clear()
    current_apple_tv.update(
        {"identifier": "", "address": "", "name": "", "label": ""}
    )
    receiver_http_host["host"] = ""
    apple_tv_auto_state["content_key"] = None
    apple_tv_auto_state["tmdb_key"] = None
    apple_tv_auto_state["query"] = None
    apple_tv_auto_state["last_metadata"] = None
    apple_tv_auto_state["last_tmdb_fetch_input"] = None
    apple_tv_auto_state["last_tmdb_fetch_refined"] = None
    apple_tv_auto_state["last_tmdb_fetch_prefer"] = None
    apple_tv_dashboard_track["last_poll_ok"] = None
    apple_tv_dashboard_track["consecutive_fail"] = 0
    if playback_overlay_widget is not None:
        playback_overlay_widget.clear_cache()
    describe_current_apple_tv()
    _refresh_location_selector()
    _rebuild_paired_devices_panel()
    _schedule_refresh_pairing_leds()
    try:
        _warm_playback_overlay_blits()
    except Exception:
        pass
    skip_cache[0] = None
    try:
        render_once()
    except Exception:
        pass
    tail = f"{msg1}\n{msg2}"
    if cred_err:
        tail += f"\nCredentials file: {cred_err}"
    if ok1 and ok2 and not cred_err:
        messagebox.showinfo("Reset", tail)
    else:
        messagebox.showwarning("Reset", tail)


def _enter_main_settings_for_rotary(*, DevPhase, dev_phase, main_settings_widget, render_once, skip_cache, sync_developer_chrome) -> bool:
    """Bring up main settings so the encoder can drive the new menus."""
    if main_settings_widget is None:
        return False
    if dev_phase[0] == DevPhase.MAIN_SETTINGS:
        return True
    try:
        if main_settings_widget.state.keyboard_open:
            main_settings_widget.state.close_keyboard(commit=False)
            main_settings_widget.invalidate()
    except Exception:
        pass
    try:
        from pigeon.widgets.ui_color_settings import load_persisted_theme_into_state

        load_persisted_theme_into_state(main_settings_widget.state)
        main_settings_widget.invalidate()
    except Exception:
        pass
    dev_phase[0] = DevPhase.MAIN_SETTINGS
    try:
        main_settings_widget.prefetch_scans_for_settings()
    except Exception:
        pass
    try:
        from pigeon.weather import DEFAULT_WEATHER_ZIP, refresh_weather

        refresh_weather(zip_code=DEFAULT_WEATHER_ZIP, force=True)
    except Exception:
        pass
    skip_cache[0] = None
    sync_developer_chrome()
    render_once()
    return True
