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
from pigeon.app_state import advance_delegation_active
from pigeon.app_state import append_delegation_log_lines
from pigeon.app_state import read_current_location_id
from pigeon.app_state import read_saved_streaming_devices_all
from pigeon.clock_saver_policy import tmdb_should_skip_refetch_on_resume


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


def _apple_tv_auto_poll_tick(*, APPLE_TV_FAIL_POLL_MAX_MS, APPLE_TV_IDLE_POLL_MS, APPLE_TV_POLL_MS, RECEIVER_POLL_MS, _PIGEON_EXT, _apple_tv_auto_poll_tick, _apple_tv_scan_timeout_s, _atv_metadata_is_content_idle, _bump_clock_saver_significant_device, _bump_clock_saver_significant_device_from_metadata, _clear_displayed_tmdb_art_for_content_change, _content_key_from_metadata, _idle_audio_meter_active, _pyatv_install_hint, _refresh_content_indicator, _refresh_observed_pairing_led_rows, _return_to_landing_if_atv_idle, _schedule_hdmi_frame_check_from_poll, _seed_current_apple_tv_from_streaming_slot, _store_music_artwork_from_metadata, _sync_status_bar_visibility_for_playback, _sync_streaming_badge_from_playback_sources, _tmdb_pref_from_metadata, _tmdb_spawn_identity_changed, _update_atv_interaction_from_poll_metadata, _update_status_bar_from_metadata, _warm_playback_overlay_blits, active_tmdb_title_key, apple_tv_auto_state, apple_tv_busy, apple_tv_dashboard_track, current_apple_tv, denon_vol_cache, playback_overlay_widget, receiver_overlay_state, receiver_standby_holder, render_once, resolve_metadata_tmdb_query, root, skip_cache, spawn_tmdb_poster_fetch, streaming_slot_holder) -> None:
    if apple_tv_auto_state.get("running"):
        root.after(APPLE_TV_POLL_MS, _apple_tv_auto_poll_tick)
        return
    # Avoid overlapping pyatv scan/connect with discover / pairing / probe — reduces spurious TV pairing prompts.
    if apple_tv_busy["active"]:
        root.after(APPLE_TV_POLL_MS, _apple_tv_auto_poll_tick)
        return
    if not current_apple_tv.get("identifier"):
        _seed_current_apple_tv_from_streaming_slot()
    if not current_apple_tv.get("identifier") or not _PIGEON_EXT:
        _sync_streaming_badge_from_playback_sources(None)
        _sync_status_bar_visibility_for_playback(None)
        if _PIGEON_EXT:
            try:
                _schedule_hdmi_frame_check_from_poll()
            except Exception:
                pass
        root.after(APPLE_TV_POLL_MS, _apple_tv_auto_poll_tick)
        return
    apple_tv_auto_state["running"] = True
    device_identifier = current_apple_tv.get("identifier", "")
    device_address = current_apple_tv.get("address", "")

    def worker() -> None:
        try:
            from pigeon.apple_tv_now_playing import fetch_now_playing_info_for_device

            ok_w, msg_w, metadata_w = fetch_now_playing_info_for_device(
                device_identifier=device_identifier,
                device_address=device_address,
                scan_timeout_s=_apple_tv_scan_timeout_s(),
            )
        except ImportError:
            ok_w, msg_w, metadata_w = (
                False,
                _pyatv_install_hint(),
                None,
            )
        except Exception as e:
            ok_w, msg_w, metadata_w = False, str(e), None

        # Roku ECP calls can block for multi-second HTTP timeouts; never run them on the Tk thread
        # or the whole UI (including the mic visualizer) freezes on every poll cadence.
        wk_roku_nm: str | None = None
        md_poll_w = metadata_w if isinstance(metadata_w, dict) else None
        md_act_w = md_poll_w is not None and not _atv_metadata_is_content_idle(md_poll_w)
        pyatv_has_app_w = False
        if md_act_w:
            pyatv_has_app_w = bool(
                str(md_poll_w.get("app_name") or "").strip()
                or str(md_poll_w.get("app_id") or "").strip()
            )
        if not pyatv_has_app_w:
            try:
                from pigeon.roku_ecp import (
                    fetch_roku_active_app_name,
                    resolve_roku_ecp_base_url_for_row,
                )

                row0_wk = streaming_slot_holder[0]
                rb_wk = resolve_roku_ecp_base_url_for_row(row0_wk) if row0_wk else ""
                if rb_wk:
                    t_nm = fetch_roku_active_app_name(rb_wk)
                    if t_nm:
                        wk_roku_nm = t_nm
            except Exception:
                wk_roku_nm = None

        pyatv_tmdb_eligible_w = False
        if isinstance(metadata_w, dict):
            from pigeon.tmdb_poster import is_degenerate_tmdb_query

            _q_wk = (
                resolve_metadata_tmdb_query(metadata_w)
                if resolve_metadata_tmdb_query is not None
                else str(metadata_w.get("query") or "").strip()
            )
            if (
                _q_wk
                and not is_degenerate_tmdb_query(_q_wk)
                and not _atv_metadata_is_content_idle(metadata_w)
            ):
                pyatv_tmdb_eligible_w = True

        wk_roku_title: tuple[bool, str, str | None] | None = None
        if not pyatv_tmdb_eligible_w:
            row0_nf_wk = streaming_slot_holder[0]
            if row0_nf_wk is not None and not row_is_playback_apple_tv(row0_nf_wk):
                try:
                    from pigeon.roku_ecp import (
                        fetch_roku_title_for_metadata,
                        resolve_roku_ecp_base_url_for_row,
                    )

                    rb_nf_wk = resolve_roku_ecp_base_url_for_row(row0_nf_wk)
                    if rb_nf_wk:
                        wk_roku_title = fetch_roku_title_for_metadata(rb_nf_wk, timeout=6.0)
                except Exception:
                    wk_roku_title = None

        def finish() -> None:
            apple_tv_auto_state["running"] = False
            meter_up = bool(_idle_audio_meter_active())
            pyatv_ok = bool(ok_w and isinstance(metadata_w, dict))
            md_for_status: dict[str, object] | None = metadata_w if pyatv_ok else None
            next_poll_ms = APPLE_TV_POLL_MS
            if current_apple_tv.get("identifier"):
                if pyatv_ok:
                    apple_tv_dashboard_track["last_poll_ok"] = True
                    apple_tv_dashboard_track["consecutive_fail"] = 0
                    if isinstance(metadata_w, dict) and _atv_metadata_is_content_idle(metadata_w):
                        # Idle Apple TV metadata does not need aggressive 3s reconnect cadence.
                        next_poll_ms = max(APPLE_TV_POLL_MS, APPLE_TV_IDLE_POLL_MS)
                else:
                    apple_tv_dashboard_track["last_poll_ok"] = False
                    apple_tv_dashboard_track["consecutive_fail"] = int(
                        apple_tv_dashboard_track.get("consecutive_fail", 0)
                    ) + 1
                    cf = int(apple_tv_dashboard_track.get("consecutive_fail", 0) or 0)
                    if cf == 1 or cf % 5 == 0:
                        try:
                            from pigeon.pi_diagnostics import append_pigeon_log

                            append_pigeon_log(
                                f"metadata poll failed ({cf}×): {str(msg_w or '')[:240]}"
                            )
                        except Exception:
                            pass
                    # Back off repeated connect attempts to reduce socket churn and UI pressure.
                    next_poll_ms = min(
                        APPLE_TV_FAIL_POLL_MAX_MS,
                        APPLE_TV_POLL_MS * max(2, min(cf, 5)),
                    )
                    if cf == 3:
                        try:
                            from device_capability_matrix import (
                                FEATURES as _delegation_feature_rows,
                                active_device_columns as _adv_dev_cols,
                            )

                            lid_log = str(read_current_location_id() or "").strip()
                            if lid_log:
                                _poll_entries = [
                                    (
                                        str(fid),
                                        f"{_fn}: player poll failed ({cf}\u00d7) while using this "
                                        f"delegation chain \u2014 {str(msg_w or '')[:120]}",
                                    )
                                    for _fn, fid in _delegation_feature_rows
                                ]
                                append_delegation_log_lines(lid_log, _poll_entries)
                                ndev = len(_adv_dev_cols())
                                if ndev > 1:
                                    advance_delegation_active(lid_log, "title", ndev)
                        except Exception:
                            pass
            if not meter_up:
                try:
                    from pigeon.observed_capability import (
                        update_observed_capabilities_from_player_poll,
                    )

                    _lid_ob = str(read_current_location_id() or "").strip()
                    if _lid_ob and device_identifier and device_address:
                        row_poll: dict[str, str] | None = None
                        for _r in read_saved_streaming_devices_all():
                            if (
                                str(_r.get("identifier") or "").strip()
                                == str(device_identifier).strip()
                                and str(_r.get("address") or "").strip()
                                == str(device_address).strip()
                            ):
                                row_poll = dict(_r)
                                break
                        if row_poll is None:
                            row_poll = {
                                "identifier": str(device_identifier).strip(),
                                "address": str(device_address).strip(),
                                "name": str(current_apple_tv.get("name") or "").strip(),
                                "label": str(current_apple_tv.get("label") or "").strip(),
                            }
                        update_observed_capabilities_from_player_poll(
                            _lid_ob,
                            row_poll,
                            ok=bool(ok_w),
                            metadata=metadata_w if isinstance(metadata_w, dict) else None,
                        )
                except Exception:
                    pass
                _refresh_observed_pairing_led_rows()
                _refresh_content_indicator()
            md_for_spawn: dict[str, object] | None = None
            if metadata_w:
                if ok_w:
                    _update_atv_interaction_from_poll_metadata(metadata_w)
                prefer_snap = _tmdb_pref_from_metadata(metadata_w)
                _ppm = str(metadata_w.get("prefer_pyatv_media") or "").strip().lower()
                if _ppm not in ("auto", "tv", "movie"):
                    _ppm = "auto"
                # Keep full poll dict for view-4 diagnostics; normalize the fields Pigeon logic relies on.
                merged_md: dict[str, object] = dict(metadata_w)
                pyatv_query = str(metadata_w.get("query") or "").strip()
                resolved_query = (
                    resolve_metadata_tmdb_query(metadata_w)
                    if resolve_metadata_tmdb_query is not None
                    else pyatv_query
                )
                merged_md["query"] = pyatv_query or resolved_query
                merged_md["title"] = str(metadata_w.get("title") or "").strip()
                merged_md["artist"] = str(metadata_w.get("artist") or "").strip()
                merged_md["series_name"] = str(metadata_w.get("series_name") or "").strip()
                merged_md["album"] = str(metadata_w.get("album") or "").strip()
                merged_md["media_type"] = str(metadata_w.get("media_type") or "").strip()
                merged_md["total_time"] = metadata_w.get("total_time")
                merged_md["position"] = metadata_w.get("position")
                merged_md["device_state"] = str(metadata_w.get("device_state") or "").strip()
                merged_md["power_state"] = str(metadata_w.get("power_state") or "").strip()
                merged_md["inferred_prefer"] = prefer_snap
                merged_md["prefer_pyatv_media"] = _ppm
                merged_md["content_key"] = _content_key_from_metadata(merged_md)
                merged_md["app_name"] = str(metadata_w.get("app_name") or "").strip()
                merged_md["app_id"] = str(metadata_w.get("app_id") or "").strip()
                merged_md["volume_percent"] = metadata_w.get("volume_percent")
                prev_md = apple_tv_auto_state.get("last_metadata")
                try:
                    from pigeon.display_confidence import (
                        hold_identity_across_idle_poll,
                    )

                    merged_md = hold_identity_across_idle_poll(
                        prev_md if isinstance(prev_md, dict) else None,
                        merged_md,
                    )
                    merged_md["content_key"] = _content_key_from_metadata(
                        merged_md
                    )
                except Exception:
                    pass
                if ok_w:
                    _bump_clock_saver_significant_device_from_metadata(merged_md)
                md_for_spawn = merged_md
                try:
                    from pigeon.display_confidence import (
                        PYATV_IDENTITY,
                        mark_identity,
                        mark_stale,
                        player_metadata_adequate,
                    )
                    prev_app = ""
                    if isinstance(prev_md, dict):
                        prev_app = str(
                            prev_md.get("app_id")
                            or prev_md.get("app_name")
                            or ""
                        ).strip().casefold()
                    new_app = str(
                        merged_md.get("app_id") or merged_md.get("app_name") or ""
                    ).strip().casefold()
                    app_changed = bool(prev_app and new_app and prev_app != new_app)
                    player_ok = player_metadata_adequate(merged_md)
                    if player_ok:
                        mark_identity(
                            merged_md, source="pyatv", confidence=PYATV_IDENTITY
                        )
                        try:
                            from pigeon.title_decision import (
                                apply_decision_to_metadata,
                                record_title_decision,
                            )

                            q_py = str(
                                merged_md.get("query")
                                or merged_md.get("title")
                                or ""
                            ).strip()
                            prev_dec = str(
                                merged_md.get("title_decision_title") or ""
                            ).strip()
                            if q_py and q_py.casefold() != prev_dec.casefold():
                                decision = record_title_decision(
                                    q_py,
                                    source="pyatv",
                                    reason="player metadata supplied a usable title",
                                )
                                apply_decision_to_metadata(merged_md, decision)
                                apple_tv_auto_state["last_title_decision"] = (
                                    decision.explain()
                                )
                        except Exception:
                            pass
                    if app_changed and not player_ok:
                        # Metadata-rich app → no-meta app: drop stale
                        # identity/art.
                        mark_stale(merged_md)
                        _clear_displayed_tmdb_art_for_content_change()
                except Exception:
                    pass
                apple_tv_auto_state["last_metadata"] = merged_md
                if not meter_up:
                    try:
                        _schedule_hdmi_frame_check_from_poll()
                    except Exception:
                        pass
                    # Music artwork (bytes live only on the poll dict; not stored in last_metadata).
                    try:
                        art_md = dict(merged_md)
                        if isinstance(metadata_w, dict) and metadata_w.get("artwork_bytes"):
                            art_md["artwork_bytes"] = metadata_w.get("artwork_bytes")
                            if metadata_w.get("artwork_id"):
                                art_md["artwork_id"] = metadata_w.get("artwork_id")
                        _store_music_artwork_from_metadata(art_md)
                    except Exception:
                        pass
                    _update_status_bar_from_metadata(metadata_w)
                    if playback_overlay_widget is not None:
                        row_av = streaming_slot_holder[0]
                        if row_av and row_is_playback_apple_tv(row_av):
                            from pigeon.widgets.playback_overlay import (
                                volume_percent_to_widget_line,
                            )

                            v_line = volume_percent_to_widget_line(
                                metadata_w.get("volume_percent")
                            )
                            # The Denon poll runs on its own short cadence and is the
                            # authoritative source whenever it has produced a usable
                            # reading recently — Apple TV's ``volume_percent`` reads 0
                            # when a physical AV receiver owns the volume, which would
                            # otherwise flash "0" over the correct dB value every
                            # metadata tick. Keep polling (scale-change detection stays
                            # active) but do not let that poll update the widget while
                            # Denon still owns the line.
                            last_denon_usable = float(
                                denon_vol_cache.get("mono_usable") or 0.0
                            )
                            denon_staleness_s = time.monotonic() - last_denon_usable
                            denon_authoritative = (
                                not receiver_standby_holder[0]
                                and bool(denon_vol_cache.get("effective"))
                                and denon_staleness_s
                                < (RECEIVER_POLL_MS / 1000.0) * 6
                            )
                            if v_line and not denon_authoritative:
                                old_v = str(receiver_overlay_state.get("volume", ""))
                                if old_v != v_line:
                                    receiver_overlay_state["volume"] = v_line
                                    _bump_clock_saver_significant_device()
                                    _warm_playback_overlay_blits()
                                    skip_cache[0] = None
                                    render_once()
            md_poll = metadata_w if isinstance(metadata_w, dict) else None
            _sync_streaming_badge_from_playback_sources(
                md_poll,
                roku_app_name=wk_roku_nm,
            )
            pyatv_tmdb_eligible = False
            if isinstance(md_for_spawn, dict):
                from pigeon.tmdb_poster import is_degenerate_tmdb_query

                query = str(md_for_spawn.get("query") or "").strip()
                content_key = _content_key_from_metadata(md_for_spawn)
                prefer = _tmdb_pref_from_metadata(md_for_spawn)
                if (
                    query
                    and not is_degenerate_tmdb_query(query)
                    and not _atv_metadata_is_content_idle(md_for_spawn)
                ):
                    pyatv_tmdb_eligible = True
                    prev_key = apple_tv_auto_state.get("content_key")
                    content_changed = bool(content_key and content_key != prev_key)
                    if content_changed:
                        apple_tv_auto_state["content_key"] = content_key
                        # Clear prior poster/cast and unlock spawn identity so the
                        # new title can fetch (force-quit was previously the only
                        # path that cleared tmdb_key after a stuck empty state).
                        if not meter_up:
                            _clear_displayed_tmdb_art_for_content_change()
                    apple_tv_auto_state["query"] = query
                    apple_tv_auto_state["prefer"] = prefer
                    needs_spawn = bool(
                        content_changed
                        or _tmdb_spawn_identity_changed(
                            query,
                            prefer,
                            md_for_spawn,
                            prev_content_key=prev_key,
                        )
                    )
                    # Empty display with active playback: retry once per identity.
                    # After a no-match / exhausted error-flag cycle we set
                    # ``tmdb_missing_art`` so this path cannot spin forever.
                    if (
                        not needs_spawn
                        and active_tmdb_title_key[0] is None
                        and not apple_tv_auto_state.get("tmdb_fetch_in_flight")
                        and not apple_tv_auto_state.get("pending_tmdb")
                        and not apple_tv_auto_state.get("tmdb_missing_art")
                    ):
                        needs_spawn = True
                    if tmdb_should_skip_refetch_on_resume(
                        content_key=content_key,
                        prev_content_key=prev_key,
                        has_tmdb_identity=bool(
                            apple_tv_auto_state.get("tmdb_key")
                            or active_tmdb_title_key[0]
                        ),
                    ):
                        needs_spawn = False
                    if needs_spawn:
                        spawn_tmdb_poster_fetch(
                            query, prefer=prefer, force=content_changed
                        )
                if ok_w:
                    _return_to_landing_if_atv_idle(md_for_spawn)
            if not pyatv_tmdb_eligible and wk_roku_title is not None:
                try:
                    from pigeon.tmdb_poster import is_degenerate_tmdb_query

                    r_ok, _rmsg, rtitle = wk_roku_title
                    if (
                        r_ok
                        and rtitle
                        and not is_degenerate_tmdb_query(rtitle)
                    ):
                        r_md: dict[str, object] = {
                            "query": str(rtitle).strip(),
                            "title": str(rtitle).strip(),
                            "artist": "",
                            "series_name": "",
                            "album": "",
                            "media_type": "",
                            "total_time": None,
                            "position": None,
                            "device_state": "Playing",
                            "app_name": str(wk_roku_nm or ""),
                            "app_id": "",
                            "prefer_pyatv_media": "auto",
                        }
                        prefer_r = _tmdb_pref_from_metadata(r_md)
                        r_md["inferred_prefer"] = prefer_r
                        r_md["content_key"] = _content_key_from_metadata(r_md)
                        apple_tv_auto_state["last_metadata"] = r_md
                        if not meter_up:
                            _update_status_bar_from_metadata(r_md)
                        md_for_status = r_md
                        prev_rk = apple_tv_auto_state.get("content_key")
                        r_ck = r_md.get("content_key")
                        r_changed = bool(r_ck and r_ck != prev_rk)
                        if r_changed:
                            apple_tv_auto_state["content_key"] = r_ck
                            _clear_displayed_tmdb_art_for_content_change()
                        r_q = str(rtitle).strip()
                        apple_tv_auto_state["query"] = r_q
                        apple_tv_auto_state["prefer"] = "auto"
                        r_needs = bool(
                            r_changed
                            or _tmdb_spawn_identity_changed(
                                r_q, "auto", r_md, prev_content_key=prev_rk
                            )
                        )
                        if (
                            not r_needs
                            and active_tmdb_title_key[0] is None
                            and not apple_tv_auto_state.get("tmdb_fetch_in_flight")
                            and not apple_tv_auto_state.get("pending_tmdb")
                            and not apple_tv_auto_state.get("tmdb_missing_art")
                        ):
                            r_needs = True
                        if tmdb_should_skip_refetch_on_resume(
                            content_key=r_ck,
                            prev_content_key=prev_rk,
                            has_tmdb_identity=bool(
                                apple_tv_auto_state.get("tmdb_key")
                                or active_tmdb_title_key[0]
                            ),
                        ):
                            r_needs = False
                        if r_needs:
                            spawn_tmdb_poster_fetch(
                                r_q, prefer="auto", force=r_changed
                            )
                        if not pyatv_ok:
                            apple_tv_dashboard_track["last_poll_ok"] = True
                            apple_tv_dashboard_track["consecutive_fail"] = 0
                except Exception:
                    pass
            if not meter_up:
                _sync_status_bar_visibility_for_playback(md_for_status)
                try:
                    _schedule_hdmi_frame_check_from_poll()
                except Exception:
                    pass
            root.after(max(APPLE_TV_POLL_MS, int(next_poll_ms)), _apple_tv_auto_poll_tick)

        root.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()


def _receiver_volume_worker(*, _clock_saver_volume, _note_volume_graphics, _receiver_volume_queue, _volume_rotary_fail_log_count, _volume_rotary_ok_log_count, denon_vol_cache, receiver_overlay_state, receiver_power_on_pending, receiver_standby_holder, receiver_volume_cmd_busy, render_once, root) -> None:
    while True:
        host, action = _receiver_volume_queue.get()
        burst = [action]
        while True:
            try:
                _h, nxt = _receiver_volume_queue.get_nowait()
            except queue.Empty:
                break
            burst.append(nxt)
        receiver_volume_cmd_busy[0] = True
        vol = ""
        try:
            from pigeon.receiver_denon import (
                apply_denon_master_volume,
                coalesce_receiver_volume_actions,
            )

            steps, mutes = coalesce_receiver_volume_actions(burst)
            ok, msg, vol = apply_denon_master_volume(
                host, steps=steps, mute_toggles=mutes, timeout=4.0
            )
            if ok:
                receiver_standby_holder[0] = False
                receiver_power_on_pending[0] = False
                try:
                    from pigeon.runtime_state import update_receiver_runtime

                    update_receiver_runtime(standby=False, reachable=True)
                except Exception:
                    pass
        except Exception as exc:
            ok, msg = False, str(exc)
        finally:
            receiver_volume_cmd_busy[0] = False
        if ok and vol:
            confirmed = vol

            def _apply_confirmed_volume(v: str = confirmed) -> None:
                try:
                    denon_vol_cache["effective"] = v
                    denon_vol_cache["np_hold"] = v
                except NameError:
                    pass
                try:
                    receiver_overlay_state["volume"] = v
                except NameError:
                    pass
                try:
                    _clock_saver_volume.remember(v, source="poll")
                    _note_volume_graphics(v)
                except Exception:
                    pass
                try:
                    render_once()
                except Exception:
                    pass

            try:
                root.after(0, _apply_confirmed_volume)
            except Exception:
                pass
        if ok:
            if _volume_rotary_ok_log_count[0] < 8:
                sys.stderr.write(
                    f"pigeon: rotary_volume_gpio: receiver {burst!r}: {msg}\n"
                )
                sys.stderr.flush()
                _volume_rotary_ok_log_count[0] += 1
            continue
        if _volume_rotary_fail_log_count[0] < 16:
            sys.stderr.write(
                f"pigeon: rotary_volume_gpio: receiver {burst!r} failed: {msg}\n"
            )
            sys.stderr.flush()
            _volume_rotary_fail_log_count[0] += 1


def _receiver_volume_poll_tick(*, RECEIVER_VOLUME_POLL_MS, _PIGEON_EXT, _bind_receiver_volume_hub, _quick_receiver_volume_poll, _receiver_volume_poll_tick, receiver_http_host, root) -> None:
    root.after(RECEIVER_VOLUME_POLL_MS, _receiver_volume_poll_tick)
    if not _PIGEON_EXT:
        return
    _bind_receiver_volume_hub(str(receiver_http_host.get("host") or "").strip())
    _quick_receiver_volume_poll()
