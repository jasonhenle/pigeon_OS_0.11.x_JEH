"""Apple TV target selection, HDMI OCR scheduling, and receiver volume queueing.

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


def _apply_hdmi_ocr_clues(clues, *, _bump_clock_saver_significant_device, _content_key_from_metadata, _note_metadata_activity, _sync_now_playing_screen_state, apple_tv_auto_state, spawn_tmdb_poster_fetch) -> None:
    from pigeon.display_confidence import ocr_is_in_charge
    from pigeon.hdmi_ocr import (
        apply_clues_to_metadata,
        apply_ocr_title_as_identity,
        hdmi_capture_available,
        hdmi_last_frame_changed,
    )
    from pigeon.tmdb_poster import is_degenerate_tmdb_query

    apple_tv_auto_state["ocr_in_flight"] = False
    try:
        from pigeon.source_toggles import source_enabled

        if not source_enabled("hdmi"):
            return
        metadata_on = source_enabled("metadata")
        hdmi_on = True
    except Exception:
        metadata_on = True
        hdmi_on = True
    md = apple_tv_auto_state.get("last_metadata")
    if not isinstance(md, dict):
        md = {}
    raw_query = str(md.get("query") or "").strip()
    had_query = bool(raw_query) and not is_degenerate_tmdb_query(raw_query)
    merged = apply_clues_to_metadata(md, clues)
    identity_changed = apply_ocr_title_as_identity(merged)
    merged["content_key"] = _content_key_from_metadata(merged)
    apple_tv_auto_state["last_metadata"] = merged
    # Meaningful HDMI screen change postpones the 2-minute metadata-idle saver.
    if hdmi_last_frame_changed() or identity_changed:
        _note_metadata_activity()
        _bump_clock_saver_significant_device()
    if identity_changed:
        apple_tv_auto_state["last_title_decision"] = str(
            merged.get("title_decision") or ""
        ).strip() or apple_tv_auto_state.get("last_title_decision")
    agrees = bool(merged.get("ocr_agrees"))
    guess = str(clues.title_guess or "").strip()
    reason = str(clues.reason or "")
    ocr_owns = ocr_is_in_charge(
        merged,
        hdmi_on=hdmi_on,
        hdmi_present=hdmi_capture_available(),
    ) or (not metadata_on)
    # Player still owns a real title: OCR watches, but does not replace art.
    if metadata_on and had_query and not ocr_owns and reason in ("pause", "confirm", "watch") and agrees:
        try:
            _sync_now_playing_screen_state()
        except Exception:
            pass
        return
    if guess and not is_degenerate_tmdb_query(guess) and ocr_owns and identity_changed:
        apple_tv_auto_state["query"] = guess
        spawn_tmdb_poster_fetch(guess, prefer="auto", force=True)
    try:
        _sync_now_playing_screen_state()
    except Exception:
        pass


def _schedule_hdmi_ocr_from_poll(metadata: dict[str, object], *, _on_hdmi_ocr_clues, _tmdb_info_current_and_available, apple_tv_auto_state) -> None:
    """OCR until TMDb has a title, then every 5s to catch a new screen."""
    try:
        from pigeon.source_toggles import source_enabled

        if not source_enabled("hdmi"):
            return
    except Exception:
        pass
    try:
        from pigeon.hdmi_ocr import decide_ocr_reason, request_ocr
    except Exception:
        return
    reason = decide_ocr_reason(
        metadata,
        tmdb_up=bool(_tmdb_info_current_and_available()),
    )
    if not reason:
        return
    if apple_tv_auto_state.get("ocr_in_flight"):
        return
    apple_tv_auto_state["ocr_in_flight"] = True
    if not request_ocr(reason, _on_hdmi_ocr_clues):
        apple_tv_auto_state["ocr_in_flight"] = False


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
