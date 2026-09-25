"""Apple TV target selection, HDMI frame-check scheduling, and receiver volume queueing.

Extracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function
takes the app state it used to close over as keyword-only arguments;
``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.
"""

from __future__ import annotations

import queue
import time
from pigeon.app_state import read_last_receiver
from pigeon.app_state import read_saved_av_receiver
from pigeon.app_state import write_last_apple_tv
from pigeon.app_state import write_last_receiver
from pigeon.app_state import row_is_playback_apple_tv
from pigeon.runtime_paths import PIGEON_STATE_DIR_TILDE
import sys
import threading
import tkinter.messagebox as messagebox


def _seed_current_apple_tv_from_streaming_slot(*, current_apple_tv, streaming_slot_holder) -> None:
    """Keep the poll target in sync with the location's saved player.

    ``current_apple_tv`` is initialized from ``last_apple_tv``. That key
    can be missing (Mac state after a location-only save, or a raced
    write) even when FAMILY ROOM still has Downstairs AppleTV. Without
    an identifier the poll tick returns immediately and the inspector
    / now-playing stay empty.
    """
    row = streaming_slot_holder[0]
    if not isinstance(row, dict):
        return
    ident = str(row.get("identifier") or "").strip()
    if not ident:
        return
    if str(current_apple_tv.get("identifier") or "").strip() == ident:
        if not str(current_apple_tv.get("address") or "").strip():
            current_apple_tv["address"] = str(row.get("address") or "").strip()
        return
    current_apple_tv.clear()
    current_apple_tv.update(
        {
            "identifier": ident,
            "address": str(row.get("address") or "").strip(),
            "name": str(row.get("name") or "").strip(),
            "label": str(row.get("label") or "").strip(),
        }
    )
    write_last_apple_tv(
        identifier=ident,
        address=str(row.get("address") or "").strip(),
        name=str(row.get("name") or "").strip() or None,
        label=str(row.get("label") or "").strip() or None,
    )


def describe_current_apple_tv(*, suffix: str | None = None, _refresh_content_indicator, apple_tv_status_var, current_apple_tv, receiver_http_host) -> None:
    if current_apple_tv.get("name"):
        play = f'Playback: {current_apple_tv["name"]}'
    elif current_apple_tv.get("label"):
        play = f'Playback: {current_apple_tv["label"]}'
    else:
        play = "Playback: none"
    rh = str(receiver_http_host.get("host") or "").strip()
    if rh:
        lr = read_last_receiver()
        nm = str(lr.get("name") or lr.get("label") or rh).strip() or rh
        ov = f"Overlay: {nm}"
    else:
        ov = "Overlay: none"
    base = f"{play}  ·  {ov}"
    if suffix:
        base = f"{base} ({suffix})"
    apple_tv_status_var.set(base)
    _refresh_content_indicator()


def begin_apple_tv_operation(status_suffix: str, *, apple_tv_busy, describe_current_apple_tv, set_apple_tv_controls_enabled) -> bool:
    if apple_tv_busy["active"]:
        describe_current_apple_tv(suffix="busy")
        return False
    apple_tv_busy["active"] = True
    set_apple_tv_controls_enabled(False)
    describe_current_apple_tv(suffix=status_suffix)
    return True


def end_apple_tv_operation(*, suffix: str | None = None, apple_tv_busy, describe_current_apple_tv, set_apple_tv_controls_enabled) -> None:
    apple_tv_busy["active"] = False
    set_apple_tv_controls_enabled(True)
    describe_current_apple_tv(suffix=suffix)


def _apply_hdmi_frame_check(changed, *, _bump_clock_saver_significant_device, _note_metadata_activity, _sync_now_playing_screen_state, apple_tv_auto_state) -> None:
    """A changed HDMI picture postpones the 2-minute metadata-idle saver."""
    apple_tv_auto_state["hdmi_check_in_flight"] = False
    if changed:
        _note_metadata_activity()
        _bump_clock_saver_significant_device()
    try:
        _sync_now_playing_screen_state()
    except Exception:
        pass


def _schedule_hdmi_frame_check_from_poll(*, _on_hdmi_frame_checked, apple_tv_auto_state) -> None:
    """Fingerprint an HDMI frame every few seconds (feeds the HDMI clock saver)."""
    try:
        from pigeon.hdmi_capture import frame_check_due, request_frame_check
    except Exception:
        return
    if apple_tv_auto_state.get("hdmi_check_in_flight"):
        return
    if not frame_check_due():
        return
    apple_tv_auto_state["hdmi_check_in_flight"] = True
    if not request_frame_check(_on_hdmi_frame_checked):
        apple_tv_auto_state["hdmi_check_in_flight"] = False


def _queue_receiver_volume_action(action: str, *, _receiver_volume_queue, avr_slot_holder) -> bool:
    row = avr_slot_holder[0]
    if not row:
        return False
    host = str(row.get("address") or row.get("identifier") or "").strip()
    if not host:
        return False
    try:
        _receiver_volume_queue.put_nowait((host, action))
    except queue.Full:
        try:
            _receiver_volume_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _receiver_volume_queue.put_nowait((host, action))
        except queue.Full:
            pass
    return True


def _note_volume_source_lines(*, telnet_line: str = "", http_line: str = "", denon_vol_cache) -> None:
    """Remember the last observed telnet / AppCommand readouts and when they moved."""
    from pigeon.receiver_denon import _volume_readout_same

    tn = str(telnet_line or "").strip()
    http = str(http_line or "").strip()
    now = time.monotonic()
    if tn:
        prev = str(denon_vol_cache.get("last_telnet") or "")
        denon_vol_cache["last_telnet"] = tn
        if not prev or not _volume_readout_same(tn, prev):
            denon_vol_cache["last_telnet_mono"] = now
    if http:
        prev_h = str(denon_vol_cache.get("last_appcommand") or "")
        denon_vol_cache["last_appcommand"] = http
        if not prev_h or not _volume_readout_same(http, prev_h):
            denon_vol_cache["last_appcommand_mono"] = now


def _bind_receiver_volume_hub(host: str, *, _on_denon_telnet_volume) -> None:
    h = str(host or "").strip()
    if not h:
        try:
            row = read_saved_av_receiver()
            h = str((row or {}).get("address") or "").strip()
        except Exception:
            h = ""
    if not h:
        return
    try:
        from pigeon.receiver_denon_telnet import start_denon_telnet_hub

        start_denon_telnet_hub(h, on_change=_on_denon_telnet_volume)
    except Exception:
        pass


def _denon_telnet_audio_fallback(*, receiver_telnet_debug_holder) -> tuple[str, str]:
    """Telnet snapshot when HTTP/XML left incoming/config empty."""
    dbg = receiver_telnet_debug_holder[0]
    if not isinstance(dbg, dict) or not dbg:
        return "", ""
    inc = str(
        dbg.get("SYSDA") or dbg.get("SSINFAISFOR") or dbg.get("DC") or ""
    ).strip()
    cfg = str(dbg.get("MS") or "").strip()
    if inc:
        inc = inc.lower()
    if cfg:
        cfg = cfg.lower()
    return inc, cfg


def _resolve_receiver_lines_for_now_playing(*, _clock_saver_volume_raw, _denon_telnet_audio_fallback, apple_tv_auto_state, compose_playback_volume_widget_line, denon_vol_cache, receiver_overlay_state, receiver_standby_holder, streaming_slot_holder) -> tuple[str, str, str]:
    """Incoming/config/volume for View 1, with Denon telnet fallback."""
    standby = bool(receiver_standby_holder[0])
    inc = ""
    cfg = ""
    if not standby:
        inc = str(receiver_overlay_state.get("incoming") or "").strip()
        cfg = str(receiver_overlay_state.get("config") or "").strip()
        if not inc and not cfg:
            fb_inc, fb_cfg = _denon_telnet_audio_fallback()
            inc, cfg = fb_inc, fb_cfg
    vol = _clock_saver_volume_raw()
    if not vol and compose_playback_volume_widget_line is not None:
        vol = compose_playback_volume_widget_line(
            stream_row=streaming_slot_holder[0],
            apple_tv_last_metadata=apple_tv_auto_state.get("last_metadata")
            if isinstance(apple_tv_auto_state.get("last_metadata"), dict)
            else None,
            denon_vol_effective=str(denon_vol_cache.get("effective") or ""),
            roku_tv_volume_percent="",
        )
    if not vol:
        raw_vol = str(receiver_overlay_state.get("volume") or "").strip()
        if raw_vol:
            # Accept dB, mute, percent, and bare 0–100 (player-reported).
            low = raw_vol.lower()
            if (
                low in ("mute", "muted", "off")
                or "db" in low
                or raw_vol.endswith("%")
                or (raw_vol.isdigit() and 0 <= int(raw_vol) <= 100)
                or raw_vol[:1] in "+-"
            ):
                vol = raw_vol
    if vol:
        denon_vol_cache["np_hold"] = vol
    else:
        # Keep last good readout so zone3 does not flash an empty ring
        # between AVR polls / while Apple TV reports volume_percent=0.
        held = str(denon_vol_cache.get("np_hold") or "").strip()
        if not held:
            held = str(denon_vol_cache.get("effective") or "").strip()
        vol = held
    return inc, cfg, vol


def _resolve_receiver_input_label(*, receiver_overlay_state, receiver_standby_holder, receiver_telnet_debug_holder) -> str:
    """Current AVR input label for the volume-widget caption."""
    if receiver_standby_holder[0]:
        return ""
    lab = str(receiver_overlay_state.get("input") or "").strip()
    if lab:
        return lab
    dbg = receiver_telnet_debug_holder[0]
    if isinstance(dbg, dict) and dbg:
        try:
            from pigeon.receiver_denon import pick_receiver_input_label

            return pick_receiver_input_label(dbg)
        except Exception:
            return ""
    return ""


def _on_hdmi_frame_checked(changed, *, _apply_hdmi_frame_check, apple_tv_auto_state, root) -> None:
    """Hand a finished HDMI frame check back to the Tk thread."""
    try:
        root.after(0, lambda c=changed: _apply_hdmi_frame_check(c))
    except Exception:
        # Tk gone / shutdown: clear the gate so checks are not stuck off forever.
        apple_tv_auto_state["hdmi_check_in_flight"] = False


def set_current_receiver_only(row: dict[str, str], *, persist: bool = True, _rebuild_paired_devices_panel, _schedule_refresh_pairing_leds, _warm_playback_overlay_blits, describe_current_apple_tv, playback_overlay_widget, receiver_http_host, render_once, skip_cache) -> None:
    """Persist AVR / AirPlay-only row for Denon HTTP overlay only; does not change Apple TV playback."""
    adr = str(row.get("address") or "").strip()
    if not adr:
        return
    if persist:
        write_last_receiver(
            host=adr,
            name=str(row.get("name") or "").strip() or None,
            label=str(row.get("label") or "").strip() or None,
            device_id=str(row.get("identifier") or "").strip() or None,
        )
    receiver_http_host["host"] = adr
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
    describe_current_apple_tv()
    _rebuild_paired_devices_panel()
    _schedule_refresh_pairing_leds()


def on_apple_tv_selected_then_tmdb(*, _PIGEON_EXT, _open_find_device_dialog, _pyatv_install_hint, apple_tv_busy, begin_apple_tv_operation, describe_current_apple_tv, end_apple_tv_operation, last_atv_interaction_mono, root, set_current_apple_tv, spawn_tmdb_poster_fetch, streaming_slot_holder) -> None:
    """Use the saved streaming slot: pyatv (Apple TV) or Roku ECP, then TMDb + backdrop."""
    if not _PIGEON_EXT:
        messagebox.showinfo("Devices", "Pigeon extensions not loaded.")
        return
    if apple_tv_busy["active"]:
        describe_current_apple_tv(suffix="busy")
        return
    row = streaming_slot_holder[0]
    if row is None:
        _open_find_device_dialog()
        return
    if not begin_apple_tv_operation("detecting content"):
        return

    def worker() -> None:
        ok_w, msg_w, title_w = False, "", None
        if row_is_playback_apple_tv(row):
            try:
                from pigeon.apple_tv_now_playing import fetch_now_playing_title_for_device

                ok_w, msg_w, title_w = fetch_now_playing_title_for_device(
                    device_identifier=row["identifier"],
                    device_address=row["address"],
                )
            except ImportError:
                ok_w, msg_w, title_w = (
                    False,
                    _pyatv_install_hint(),
                    None,
                )
            except Exception as e:
                ok_w, msg_w, title_w = False, str(e), None
        else:
            try:
                from pigeon.roku_ecp import (
                    fetch_roku_title_for_metadata,
                    resolve_roku_ecp_base_url_for_row,
                )

                rbase = resolve_roku_ecp_base_url_for_row(row)
                if not rbase:
                    ok_w, msg_w, title_w = (
                        False,
                        "",
                        None,
                    )
                else:
                    ok_w, msg_w, title_w = fetch_roku_title_for_metadata(
                        rbase, timeout=10.0
                    )
            except Exception as e:
                ok_w, msg_w, title_w = False, str(e), None

        def finish() -> None:
            if not row_is_playback_apple_tv(row) and not ok_w and not msg_w:
                end_apple_tv_operation()
                messagebox.showinfo(
                    "Devices",
                    "This Player is not an Apple TV (pyatv) row, and Pigeon could not use "
                    "Roku ECP on its IP (port 8060).\n\n"
                    "• If this is a Roku / Roku TV (e.g. Onn), ensure the TV’s IP is in the "
                    "Player slot and try again, or set \"roku_ecp_base_url\" in "
                    f"{PIGEON_STATE_DIR_TILDE}/state.json to http://TV_IP:8060\n"
                    "• For an actual Apple TV, re-add it from Find devices so the label "
                    "shows “Apple TV / tvOS”.\n"
                    "• For a receiver only, choose Receiver in Find device for the overlay.",
                )
                return
            if not ok_w:
                end_apple_tv_operation()
                messagebox.showerror("Devices", msg_w or "Could not read now playing.")
                return
            if not title_w:
                end_apple_tv_operation()
                messagebox.showinfo(
                    "Devices",
                    msg_w or "No title reported by the selected device.",
                )
                return
            from pigeon.tmdb_poster import is_degenerate_tmdb_query

            if is_degenerate_tmdb_query(title_w):
                end_apple_tv_operation()
                messagebox.showinfo(
                    "Devices",
                    "The device only reported app or channel branding, not the show or movie "
                    "title, so Pigeon did not search TMDb.\n\n"
                    "On Disney+ via Roku, wait until playback has started and try Manual fetch again.",
                )
                return
            set_current_apple_tv(row, persist=True)
            last_atv_interaction_mono[0] = time.monotonic()
            end_apple_tv_operation(suffix="title detected")
            sys.stderr.write(f"pigeon: {msg_w}\n")
            sys.stderr.flush()
            spawn_tmdb_poster_fetch(title_w, prefer="auto", force=True)

        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def on_debug_streaming_slot_apple_tv(*, _PIGEON_EXT, _open_find_device_dialog, _pyatv_install_hint, apple_tv_busy, begin_apple_tv_operation, describe_current_apple_tv, end_apple_tv_operation, root, streaming_slot_holder) -> None:
    if not _PIGEON_EXT:
        messagebox.showinfo("Devices", "Pigeon extensions not loaded.")
        return
    if apple_tv_busy["active"]:
        describe_current_apple_tv(suffix="busy")
        return
    row = streaming_slot_holder[0]
    if row is None:
        _open_find_device_dialog()
        return
    if not row_is_playback_apple_tv(row):
        messagebox.showinfo(
            "Devices",
            "Metadata debug applies to Apple TV rows (label shows “Apple TV / tvOS”), not receivers.",
        )
        return
    if not begin_apple_tv_operation("debugging metadata"):
        return

    def worker() -> None:
        try:
            from pigeon.apple_tv_now_playing import debug_metadata_for_device

            ok_w, dump_w = debug_metadata_for_device(
                device_identifier=row["identifier"],
                device_address=row["address"],
            )
        except ImportError:
            ok_w, dump_w = (
                False,
                _pyatv_install_hint(),
            )
        except Exception as e:
            ok_w, dump_w = False, str(e)

        def finish() -> None:
            title = "Apple TV Metadata Debug"
            end_apple_tv_operation()
            if ok_w:
                messagebox.showinfo(title, dump_w)
            else:
                messagebox.showerror(title, dump_w)

        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def _send_player_play_pause_hotkey(*, _PIGEON_EXT, apple_tv_busy, current_apple_tv, streaming_slot_holder) -> bool:
    """
    If a **Player** slot is set, send play/pause on a worker thread (Apple TV: pyatv;
    Roku: ECP). Returns True when a send was queued (so Space should not fall through).
    """
    if not _PIGEON_EXT:
        return False
    if apple_tv_busy["active"]:
        return False
    row = streaming_slot_holder[0]
    if not row:
        return False
    if row_is_playback_apple_tv(row):
        ident = str(current_apple_tv.get("identifier") or "").strip() or str(
            row.get("identifier") or ""
        ).strip()
        addr = str(current_apple_tv.get("address") or "").strip() or str(
            row.get("address") or ""
        ).strip()
        if not ident:
            return False
        if not addr:
            addr = ident

        try:
            from pigeon.apple_tv_now_playing import enqueue_apple_tv_remote_command

            if enqueue_apple_tv_remote_command(
                device_identifier=ident,
                device_address=addr,
                method_name="play_pause",
                scan_timeout_s=3,
            ):
                return True
        except Exception:
            pass
        return False
    try:
        from pigeon.roku_ecp import resolve_roku_ecp_base_url_for_row, roku_send_play_pause

        rbase = str(resolve_roku_ecp_base_url_for_row(row) or "").strip()
        if not rbase:
            return False
    except Exception:
        return False

    def _work_roku() -> None:
        try:
            from pigeon.roku_ecp import roku_send_play_pause

            roku_send_play_pause(base_url=rbase, timeout=3.0)
        except Exception:
            pass

    threading.Thread(target=_work_roku, daemon=True).start()
    return True


def _commit_receiver_volume(vol: str, *, _clock_saver_volume, _note_volume_graphics, _remember_clock_saver_volume, denon_vol_cache, receiver_overlay_state) -> bool:
    """Write the box-3 AVR level into the widgets. True if it changed."""
    line = str(vol or "").strip()
    if not line:
        return False
    try:
        from pigeon.widgets.playback_overlay import _receiver_volume_display_line

        norm = _receiver_volume_display_line(line)
        if norm:
            line = norm
    except Exception:
        pass
    try:
        if _clock_saver_volume.is_stale_poll(line):
            return False
    except Exception:
        pass
    prev = ""
    try:
        prev = str(denon_vol_cache.get("effective") or "")
    except NameError:
        prev = ""
    try:
        denon_vol_cache["effective"] = line
        denon_vol_cache["np_hold"] = line
    except NameError:
        pass
    try:
        receiver_overlay_state["volume"] = line
    except NameError:
        pass
    _remember_clock_saver_volume(line, source="poll")
    _note_volume_graphics(line)
    return prev != line


def _on_denon_telnet_volume(fields: dict[str, object], *, _clock_saver_for_compose, _commit_receiver_volume, _idle_audio_meter_active, _note_volume_source_lines, _sync_now_playing_screen_state, _view_one_uses_now_playing_screen, _volume_lines, clock_saver_force_on, render_once, root, skip_cache) -> None:
    """Unsolicited telnet ``MV`` (IR / knob / HEOS) — paint immediately."""
    from pigeon.receiver_denon import _volume_fields_line

    line = _volume_fields_line({str(k): str(v) for k, v in fields.items()})
    if not line:
        return

    def apply() -> None:
        _note_volume_source_lines(telnet_line=line)
        changed = _commit_receiver_volume(line)
        if not changed and not _volume_lines.fading():
            return
        if _idle_audio_meter_active():
            return
        skip_cache[0] = None
        try:
            if _view_one_uses_now_playing_screen() and not (
                _clock_saver_for_compose(time.monotonic())
                or clock_saver_force_on[0]
            ):
                _sync_now_playing_screen_state()
        except Exception:
            pass
        try:
            render_once()
        except Exception:
            pass

    try:
        root.after(0, apply)
    except Exception:
        pass


def _quick_receiver_volume_poll(*, _clock_saver_for_compose, _commit_receiver_volume, _idle_audio_meter_active, _note_volume_source_lines, _sync_now_playing_screen_state, _view_one_uses_now_playing_screen, _volume_lines, _volume_quick_busy, clock_saver_force_on, denon_vol_cache, receiver_http_host, render_once, root, skip_cache) -> None:
    """Telnet hub + AppCommand — a moving source updates the disc."""
    if _volume_quick_busy[0]:
        return
    host = str(receiver_http_host.get("host") or "").strip()
    if not host:
        try:
            row = read_saved_av_receiver()
            host = str((row or {}).get("address") or "").strip()
        except Exception:
            host = ""
    if not host:
        return
    _volume_quick_busy[0] = True

    def work() -> None:
        vol = ""
        src = ""
        try:
            from pigeon.receiver_denon import (
                coalesce_receiver_volume_read,
                observe_receiver_volume,
            )

            tn_line, ac_line = observe_receiver_volume(
                host,
                timeout=1.0,
                telnet_blocking=True,
                allow_appcommand=True,
            )
            vol, src = coalesce_receiver_volume_read(
                telnet_line=tn_line,
                http_line=ac_line,
                last_http=str(denon_vol_cache.get("last_appcommand") or ""),
                last_telnet=str(denon_vol_cache.get("last_telnet") or ""),
                held=str(
                    denon_vol_cache.get("effective")
                    or denon_vol_cache.get("np_hold")
                    or ""
                ),
                last_http_mono=float(
                    denon_vol_cache.get("last_appcommand_mono") or 0.0
                ),
                last_telnet_mono=float(
                    denon_vol_cache.get("last_telnet_mono") or 0.0
                ),
            )
            _note_volume_source_lines(telnet_line=tn_line, http_line=ac_line)
        except Exception:
            vol, src = "", ""

        def apply() -> None:
            _volume_quick_busy[0] = False
            if not vol:
                return
            changed = _commit_receiver_volume(vol)
            if not changed and not _volume_lines.fading():
                return
            if _idle_audio_meter_active():
                return
            skip_cache[0] = None
            try:
                if _view_one_uses_now_playing_screen() and not (
                    _clock_saver_for_compose(time.monotonic())
                    or clock_saver_force_on[0]
                ):
                    _sync_now_playing_screen_state()
            except Exception:
                pass
            try:
                render_once()
            except Exception:
                pass

        try:
            root.after(0, apply)
        except Exception:
            _volume_quick_busy[0] = False

    threading.Thread(target=work, daemon=True).start()
