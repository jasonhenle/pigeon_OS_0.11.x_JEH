"""Settings panel helpers: scrolling, wheel binding, device-row parsing, pairing LEDs, tooltips and update-check UI.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

from pathlib import Path
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
