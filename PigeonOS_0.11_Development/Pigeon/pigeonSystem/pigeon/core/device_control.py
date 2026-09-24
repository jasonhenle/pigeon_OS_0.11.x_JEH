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
    try:
        from pigeon.source_toggles import source_enabled

        if not source_enabled("hdmi"):
            return
    except Exception:
        pass
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
        from pigeon.source_toggles import source_enabled

        if not source_enabled("hdmi"):
            return
    except Exception:
        pass
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
