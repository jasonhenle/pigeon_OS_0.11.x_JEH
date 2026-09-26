"""Live status for Pigeon's data sources (Wi‑Fi, player metadata, audio).

The four source tiles in settings_pigeon only *report* whether each source is
active; they are no longer on/off switches. Every source is always used.
(Older installs may still carry a ``source_toggles`` key in ``state.json``;
it is ignored and cleared by a factory reset.)
"""

from __future__ import annotations

from typing import Any


def apply_source_status_to_settings_state(state: Any) -> None:
    """Refresh the live audio flag that drives its tile LED.

    (The HDMI tile's LED is retired; HDMI presence is still tracked by
    ``pigeon.hdmi_capture`` for now-playing and the frame-change clock saver.)
    """
    try:
        from pigeon.widgets.audio_meter_saver import program_audio_present

        state.pigeon_audio_ok = bool(program_audio_present())
    except Exception:
        state.pigeon_audio_ok = False
