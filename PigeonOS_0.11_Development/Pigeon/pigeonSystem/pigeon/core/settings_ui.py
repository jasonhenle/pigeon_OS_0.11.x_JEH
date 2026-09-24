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
