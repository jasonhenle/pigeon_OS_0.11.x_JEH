"""Pigeon's main loop talks to its receiver only through ``ReceiverLink`` / ``Receiver``.

Drives the real ``device_control`` functions against a FakeReceiver — no network,
no brand module involved.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_SYS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SYS_ROOT not in sys.path:
    sys.path.insert(0, _SYS_ROOT)

from pigeon.core import device_control as dc  # noqa: E402
from pigeon.receiver.base import ReceiverState  # noqa: E402
from pigeon.receiver.fake import FakeReceiver  # noqa: E402
from pigeon.receiver_link import ReceiverLink  # noqa: E402


def wait_for(cond, timeout=2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class LinkTests(unittest.TestCase):
    def make(self):
        made: list[FakeReceiver] = []

        def factory(brand, host, **opts):
            r = FakeReceiver()
            r.host, r.opts = host, opts
            made.append(r)
            return r

        return ReceiverLink(factory), made

    def test_bind_connects_and_is_idempotent(self) -> None:
        link, made = self.make()
        rx = link.bind("10.0.0.5")
        self.assertTrue(wait_for(lambda: rx.connected))
        self.assertIs(link.bind("10.0.0.5"), rx)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].opts, {"full_poll_s": 0.75})

    def test_rebind_replaces_receiver_keeps_listeners(self) -> None:
        link, made = self.make()
        seen: list[str] = []
        link.on_volume_confirmed(seen.append)
        first = link.bind("10.0.0.5")
        wait_for(lambda: first.connected)
        second = link.bind("10.0.0.9")
        self.assertIsNot(first, second)
        self.assertFalse(first.connected)  # old one disconnected
        wait_for(lambda: second.connected)
        second.step_volume(1)
        self.assertEqual(len(seen), 1)

    def test_state_is_empty_until_bound(self) -> None:
        link, _ = self.make()
        self.assertEqual(link.state(), ReceiverState())
        self.assertFalse(link.readout_superseded("-30.0 dB"))
        self.assertIsNone(link.bind(""))


class DeviceControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rx = FakeReceiver()
        self.link = ReceiverLink(lambda brand, host, **o: self.rx)
        p = mock.patch.object(dc, "get_receiver_link", return_value=self.link)
        p.start()
        self.addCleanup(p.stop)

    def bound(self):
        self.link.bind("10.0.0.5")
        self.assertTrue(wait_for(lambda: self.rx.connected))

    def test_knob_action_changes_volume_and_wakes(self) -> None:
        self.bound()
        slot = [{"address": "10.0.0.5"}]
        pending = [False]
        before = self.rx.cached_state().volume_db
        self.assertTrue(dc._queue_receiver_volume_action("volume_up", avr_slot_holder=slot, receiver_power_on_pending=pending))
        self.assertGreater(self.rx.cached_state().volume_db, before)
        self.rx.simulate_power(False)
        self.assertTrue(dc._queue_receiver_volume_action("volume_up", avr_slot_holder=slot, receiver_power_on_pending=[True]))
        self.assertTrue(self.rx.cached_state().powered_on)

    def test_knob_action_without_saved_receiver_is_refused(self) -> None:
        self.assertFalse(dc._queue_receiver_volume_action("volume_up", avr_slot_holder=[None], receiver_power_on_pending=[False]))
        self.assertFalse(dc._queue_receiver_volume_action("volume_up", avr_slot_holder=[{"address": ""}], receiver_power_on_pending=[False]))

    def test_audio_fallback_and_input_label_come_from_receiver_state(self) -> None:
        self.bound()
        self.assertEqual(dc._receiver_audio_fallback(), ("dolby digital", "dolby surround"))
        self.assertEqual(dc._resolve_receiver_input_label(receiver_overlay_state={}, receiver_standby_holder=[False]),
                         self.rx.cached_state().input_label)
        self.assertEqual(dc._resolve_receiver_input_label(receiver_overlay_state={"input": "X"}, receiver_standby_holder=[False]), "X")
        self.assertEqual(dc._resolve_receiver_input_label(receiver_overlay_state={}, receiver_standby_holder=[True]), "")
        self.rx.simulate_power(False)
        self.assertEqual(dc._receiver_audio_fallback(), ("", ""))

    def test_callbacks_paint_confirmed_volume_and_clear_standby(self) -> None:
        self.bound()
        holders = dict(
            _clock_saver_volume=mock.Mock(), _note_volume_graphics=mock.Mock(),
            _volume_rotary_fail_log_count=[0], _volume_rotary_ok_log_count=[0],
            denon_vol_cache={}, receiver_overlay_state={}, receiver_power_on_pending=[True],
            receiver_standby_holder=[True], render_once=mock.Mock(),
            root=mock.Mock(after=lambda ms, fn: fn()),
        )
        with mock.patch("pigeon.runtime_state.update_receiver_runtime"):
            dc._register_receiver_callbacks(**holders)
            self.link.bind("10.0.0.5")  # same host: callbacks must reach the live receiver
            self.rx.step_volume(1)
        self.assertEqual(holders["receiver_overlay_state"]["volume"], "-39.5 dB")
        self.assertEqual(holders["denon_vol_cache"]["effective"], "-39.5 dB")
        self.assertEqual(holders["receiver_power_on_pending"], [False])
        self.assertEqual(holders["receiver_standby_holder"], [False])


class PollTickTests(unittest.TestCase):
    """``_receiver_poll_tick`` end to end: receiver state in, overlay state out."""

    def setUp(self) -> None:
        self.rx = FakeReceiver()
        self.link = ReceiverLink(lambda brand, host, **o: self.rx)
        self.link.bind("10.0.0.5")
        wait_for(lambda: self.rx.connected)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, kw in (
            (mock.patch.object(dc, "get_receiver_link", return_value=self.link), {}),
            (mock.patch.object(dc, "read_saved_av_receiver", return_value={"address": "10.0.0.5"}), {}),
            (mock.patch("pigeon.roku_ecp.resolve_roku_ecp_base_url", return_value=""), {}),
            (mock.patch("pigeon.runtime_state.update_receiver_runtime"), {}),
            (mock.patch("pigeon.observed_capability.update_observed_capabilities_from_receiver_poll"), {}),
        ):
            target.start()
            self.addCleanup(target.stop)

    def run_tick(self, **over):
        after_calls = []
        root = mock.Mock()
        root.after = lambda ms, fn=None, *a: (fn() if ms == 0 else after_calls.append(ms))
        saver = mock.Mock()
        saver.is_stale_poll.return_value = False
        saver.display_line.return_value = ""
        saver.hold = ""
        kw = {name: mock.Mock() for name in dc._receiver_poll_tick.__code__.co_varnames[:dc._receiver_poll_tick.__code__.co_kwonlyargcount]}
        kw.update(
            RECEIVER_POLL_MS=750, _PIGEON_EXT=True, _clock_saver_volume=saver,
            _clock_saver_for_compose=lambda now: False, _clock_saver_receiver_off=lambda: False,
            _idle_audio_meter_active=lambda: False, _view_one_uses_now_playing_screen=lambda: False,
            apple_tv_auto_state={}, avr_slot_holder=[{"address": "10.0.0.5"}], clock_saver_force_on=[False],
            denon_vol_cache={}, receiver_http_host={"host": "10.0.0.5"}, receiver_overlay_state={},
            receiver_panel_led_holder=[None], receiver_poll_busy={"active": False},
            receiver_power_on_pending=[False], receiver_power_on_until=[0.0], receiver_standby_holder=[False],
            receiver_debug_holder=[{}], skip_cache=[None], streaming_slot_holder=[None],
            _bind_receiver_volume_hub=lambda host: None, _quick_receiver_volume_poll=lambda: None,
            _receiver_poll_tick=lambda: None, root=root,
        )
        kw.update(over)
        threads = []
        real_thread = threading.Thread

        def run_inline(target=None, **k):
            t = real_thread(target=target)
            threads.append(t)
            return t

        with mock.patch.object(dc.threading, "Thread", side_effect=run_inline):
            dc._receiver_poll_tick(**kw)
        for t in threads:
            t.join(5)
        return kw

    def test_live_receiver_populates_overlay(self) -> None:
        kw = self.run_tick()
        ov = kw["receiver_overlay_state"]
        self.assertEqual(ov["incoming"], "dolby digital")
        self.assertEqual(ov["config"], "dolby surround")
        self.assertEqual(ov["input"], self.rx.cached_state().input_label)
        self.assertEqual(ov["volume"], "-40.0 dB")
        self.assertFalse(kw["receiver_standby_holder"][0])
        self.assertFalse(kw["receiver_poll_busy"]["active"])
        self.assertEqual(kw["receiver_debug_holder"][0]["sound mode"], "dolby surround")

    def test_standby_blanks_source_lines_but_keeps_volume(self) -> None:
        self.rx.simulate_power(False)
        kw = self.run_tick()
        ov = kw["receiver_overlay_state"]
        self.assertTrue(kw["receiver_standby_holder"][0])
        self.assertEqual((ov["incoming"], ov["config"], ov["input"]), ("", "", ""))

    def test_unreachable_receiver_does_not_crash_or_stick_busy(self) -> None:
        self.rx.simulate_link(False)
        self.rx.get_state()
        kw = self.run_tick()
        self.assertFalse(kw["receiver_poll_busy"]["active"])
        self.assertEqual(kw["receiver_overlay_state"].get("incoming", ""), "")

    def test_moved_receiver_is_relocated_and_saved(self) -> None:
        class Moved(FakeReceiver):
            def relocate(self, identity, *, sweep=False):
                return "10.0.0.77"

        self.rx = Moved()
        self.link = ReceiverLink(lambda brand, host, **o: self.rx)
        self.link.bind("10.0.0.5")
        self.rx.simulate_link(False)
        self.rx.get_state()
        saved = []
        with mock.patch.object(self.link, "seconds_since_bind", return_value=30.0), \
             mock.patch.object(dc, "get_receiver_link", return_value=self.link), \
             mock.patch.object(dc, "write_saved_av_receiver", side_effect=saved.append):
            kw = self.run_tick(avr_slot_holder=[{"address": "10.0.0.5"}])
        self.assertEqual(kw["receiver_http_host"]["host"], "10.0.0.77")
        self.assertEqual(self.link.host, "10.0.0.77")
        self.assertEqual(saved[0]["address"], "10.0.0.77")


if __name__ == "__main__":
    unittest.main()
