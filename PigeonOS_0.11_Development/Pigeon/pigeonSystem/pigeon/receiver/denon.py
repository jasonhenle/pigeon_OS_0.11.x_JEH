"""Denon/Marantz adapter: the existing poll, telnet hub and volume controller
behind the generic ``Receiver`` interface.

Nothing here reimplements Denon behavior. State comes from
``poll_denon_like_receiver`` plus the telnet hub's live MV/MU/PW readings;
volume and mute go through ``ReceiverVolumeController`` (coalescing,
confirmation, HTTP/HEOS fallbacks unchanged).
"""

from __future__ import annotations

import threading
import time

from pigeon.receiver.base import (
    EVT_COMMAND,
    EVT_CONNECTION,
    EVT_ERROR,
    EVT_EXTERNAL,
    EVT_RESPONSE,
    EVT_TIMEOUT,
    EventSink,
    ReceiverEvent,
    ReceiverState,
    ResultListener,
    StateListener,
    VolumeListener,
)

_TELNET_META_EVERY_S = 8.0  # full telnet metadata is occasional: it holds the one-client socket ~2s
_WAKE_HOLD_S = 12.0  # after a wake-and-act command, treat the receiver as powering up
_OWN_COMMAND_WINDOW_S = 2.0  # changes this soon after we sent something are "response"
_MAX_STEPS_PER_CALL = 24  # same bound apply_receiver_volume_once uses

# Telnet ``SI`` codes, each with a factory label; the receiver's own renames win.
_INPUT_CODES = (
    ("PHONO", "PHONO"), ("CD", "CD"), ("DVD", "DVD"), ("BD", "BLU-RAY"),
    ("GAME", "GAME"), ("MPLAY", "MEDIA PLAYER"), ("SAT/CBL", "CBL/SAT"),
    ("TV", "TV AUDIO"), ("AUX1", "AUX 1"), ("AUX2", "AUX 2"),
    ("TUNER", "TUNER"), ("NET", "NETWORK"), ("BT", "BLUETOOTH"),
)


def _telnet_audio_fallback(dbg: dict[str, str]) -> tuple[str, str]:
    """Incoming format / sound mode from a telnet snapshot when HTTP/XML left them empty."""
    if not dbg:
        return "", ""
    inc = str(dbg.get("SYSDA") or dbg.get("SSINFAISFOR") or dbg.get("DC") or "").strip().lower()
    cfg = str(dbg.get("MS") or "").strip().lower()
    return inc, cfg


class DenonReceiver:
    brand = "denon"

    def __init__(
        self,
        host: str,
        *,
        poll_fn=None,
        transport=None,
        controller=None,
        full_poll_s: float = 5.0,
        poll_timeout_s: float = 4.0,
    ) -> None:
        from pigeon.receiver_volume import DenonHubTransport, ReceiverVolumeController

        self.host = str(host or "").strip()
        self._poll_fn = poll_fn
        self.transport = transport or DenonHubTransport()
        self._controller = controller or ReceiverVolumeController(self.transport)
        self._full_poll_s = float(full_poll_s)
        self._poll_timeout_s = float(poll_timeout_s)
        self._lock = threading.RLock()
        self._state = ReceiverState()
        self._active = False
        self._reachable = False
        self._listeners: list[StateListener] = []
        self._sink: EventSink | None = None
        self._volume_listeners: list[VolumeListener] = []
        self._result_listeners: list[ResultListener] = []
        self._cmd_busy = False
        self._wake_until = 0.0
        self._telnet_meta_mono = 0.0
        self._meta_dbg: dict[str, str] = {}  # last telnet snapshot that carried audio metadata
        self._last_cmd_mono = 0.0
        self._wake = threading.Event()
        self._monitor: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._observing = False
        self._generation = 0

    # ---- Receiver interface ---------------------------------------------

    @property
    def connected(self) -> bool:
        return self._active and self._reachable

    def connect(self) -> None:
        if not self.host:
            self._emit(EVT_CONNECTION, "connection failed: no host")
            return
        with self._lock:
            self._active = True
            self._generation += 1
            gen = self._generation
        self._emit(EVT_CONNECTION, f"connecting to {self.host}")
        try:
            self.transport.ensure(self.host)
        except Exception as exc:
            self._emit(EVT_CONNECTION, f"connection failed: {exc}")
        if not self._observing:
            self._observing = True
            try:
                self.transport.add_observer(self._wake.set)
            except Exception:
                pass
        if self._worker is None:
            self._controller.on_confirmed = self._on_confirmed
            self._controller.on_result = self._on_result
            self._controller.on_busy = self._on_busy
            self._worker = threading.Thread(
                target=self._controller.run_forever, name="denon-receiver-volume", daemon=True
            )
            self._worker.start()
        self._monitor = threading.Thread(
            target=self._monitor_loop, args=(gen,), name="denon-receiver-monitor", daemon=True
        )
        self._monitor.start()
        self.get_state()

    def disconnect(self) -> None:
        with self._lock:
            self._active = False
            self._generation += 1
            self._reachable = False
        self._wake.set()
        try:
            self._controller.reset("")
        except Exception:
            pass
        self._emit(EVT_CONNECTION, "disconnected")
        self._merge(ReceiverState(connected=False), source="local")

    def get_state(self) -> ReceiverState:
        if not self._active:
            return self.cached_state()
        poll = self._poll_fn or self._default_poll
        try:
            r = poll(self.host, self._poll_timeout_s)
        except Exception as exc:
            self._emit(EVT_ERROR, f"poll raised: {exc}")
            return self.cached_state()
        if not r.ok and not self._hub_connected():
            if self._reachable:
                self._emit(EVT_TIMEOUT, f"no answer from {self.host}")
            with self._lock:
                self._reachable = False
            self._merge(ReceiverState(connected=False), source="poll")
            return self.cached_state()
        with self._lock:
            self._reachable = True
            cur = self._state
        from pigeon.receiver_denon import _volume_db_value

        muted = True if r.volume == "mute" else (False if r.volume else cur.muted)
        vol = _volume_db_value(r.volume)
        if vol is None:
            vol = cur.volume_db  # muted/blank readout: keep last level
        dbg = getattr(r, "telnet_debug", None) or {}
        incoming, config, label = r.incoming, r.config, r.input_label
        if r.ok and r.standby:
            self._meta_dbg = {}
        elif r.ok:
            if dbg.get("MS") or dbg.get("SYSDA") or dbg.get("SSINFAISFOR") or dbg.get("DC"):
                self._meta_dbg = dict(dbg)
            if not incoming and not config:
                incoming, config = _telnet_audio_fallback(self._meta_dbg)
            if not label and self._meta_dbg:
                from pigeon.receiver_denon import pick_receiver_input_label

                label = pick_receiver_input_label(self._meta_dbg)
        new = ReceiverState(
            connected=True,
            powered_on=(not r.standby) if r.ok else cur.powered_on,
            volume_db=vol,
            muted=muted,
            input_label=label if r.ok else cur.input_label,
            audio_format=incoming if r.ok else cur.audio_format,
            sound_mode=config if r.ok else cur.sound_mode,
        )
        self._merge(new, source="poll")
        return self.cached_state()

    def _default_poll(self, host: str, timeout: float):
        """The Denon poll, with full telnet metadata only when it won't starve a volume command."""
        from pigeon.receiver_denon import poll_denon_like_receiver

        now = time.monotonic()
        quiet = not self._cmd_busy and now >= self._wake_until
        use_tn = quiet and now - self._telnet_meta_mono >= _TELNET_META_EVERY_S
        r = poll_denon_like_receiver(host, timeout=timeout, include_telnet=use_tn)
        if use_tn:
            self._telnet_meta_mono = now
        return r

    def cached_state(self) -> ReceiverState:
        with self._lock:
            return self._state

    def set_power(self, on: bool) -> bool:
        return self._send("PWON" if on else "PWSTANDBY")

    def step_volume(self, steps: int, *, wake: bool = False) -> bool:
        n = max(-_MAX_STEPS_PER_CALL, min(_MAX_STEPS_PER_CALL, int(steps)))
        if not n or not self._active:
            return not n
        if wake:
            self._wake_until = time.monotonic() + _WAKE_HOLD_S
        self._note_command(f"volume {n:+d} step(s)")
        ok = True
        for _ in range(abs(n)):
            ok = self._controller.submit(self.host, "volume_up" if n > 0 else "volume_down", wake=wake) and ok
        return ok

    def toggle_mute(self, *, wake: bool = False) -> bool:
        if not self._active:
            return False
        if wake:
            self._wake_until = time.monotonic() + _WAKE_HOLD_S
        self._note_command("mute toggle")
        return self._controller.submit(self.host, "mute_toggle", wake=wake)

    # ---- InputSelectable (optional; diagnostic tool only) ----------------

    def _input_choices(self) -> list[tuple[str, str]]:
        from pigeon.receiver_denon import _input_norm, fetch_denon_source_renames

        try:
            renames = fetch_denon_source_renames(self.host)
        except Exception:
            renames = {}
        return [(renames.get(_input_norm(code)) or label, code) for code, label in _INPUT_CODES]

    def available_inputs(self) -> list[str]:
        return [label for label, _ in self._input_choices()]

    def set_input(self, label: str) -> bool:
        want = str(label or "").strip().lower()
        for name, code in self._input_choices():
            if want in (name.lower(), code.lower()):
                ok = self._send(f"SI{code}")
                if ok:
                    threading.Thread(target=self._confirm_input, daemon=True).start()
                return ok
        self._emit(EVT_ERROR, f"unknown input {label!r}")
        return False

    def add_volume_confirmed_listener(self, cb: VolumeListener) -> None:
        self._volume_listeners.append(cb)

    def add_command_result_listener(self, cb: ResultListener) -> None:
        self._result_listeners.append(cb)

    def volume_readout_superseded(self, line: str) -> bool:
        try:
            return bool(self._controller.readout_superseded(line))
        except Exception:
            return False

    # ---- Relocatable -------------------------------------------------------

    def relocate(self, identity: dict | None, *, sweep: bool = False) -> str:
        from pigeon.receiver_denon import resolve_paired_receiver_host

        try:
            return str(resolve_paired_receiver_host(identity, extra_hosts=[self.host], subnet_sweep=bool(sweep)) or "").strip()
        except Exception:
            return ""

    def _confirm_input(self) -> None:
        """Input changes aren't pushed on the hub, so poll soon instead of waiting for the next cycle."""
        before = self.cached_state().input_label
        for delay in (0.4, 0.6, 1.0):
            time.sleep(delay)
            if not self._active:
                return
            if self.get_state().input_label != before:
                return

    def add_state_listener(self, cb: StateListener) -> None:
        self._listeners.append(cb)

    def set_event_sink(self, sink: EventSink | None) -> None:
        self._sink = sink

    # ---- internals ---------------------------------------------------------

    def _hub_connected(self) -> bool:
        try:
            return bool((self.transport.state(self.host) or {}).get("connected"))
        except Exception:
            return False

    def _send(self, command: str) -> bool:
        """Same route the volume controller uses: hub socket, else HTTP AppDirect."""
        if not self._active:
            return False
        self._note_command(command)
        try:
            if self.transport.state(self.host) and self.transport.send(self.host, command, timeout=0.6) is not None:
                return True
            if self.transport.http_send(self.host, command):
                return True
        except Exception as exc:
            self._emit(EVT_ERROR, f"{command}: {exc}")
            return False
        self._emit(EVT_TIMEOUT, f"{command} not delivered")
        return False

    def _note_command(self, text: str) -> None:
        self._last_cmd_mono = time.monotonic()
        self._emit(EVT_COMMAND, text)

    def _on_confirmed(self, _host: str, line: str) -> None:
        self._emit(EVT_RESPONSE, f"volume confirmed {line}")
        self._wake.set()
        for cb in list(self._volume_listeners):
            try:
                cb(line)
            except Exception:
                pass

    def _on_result(self, _host: str, ok: bool, msg: str) -> None:
        self._emit(EVT_RESPONSE if ok else EVT_ERROR, msg)
        for cb in list(self._result_listeners):
            try:
                cb(ok, msg)
            except Exception:
                pass

    def _on_busy(self, busy: bool) -> None:
        self._cmd_busy = bool(busy)

    def _from_hub(self) -> ReceiverState | None:
        try:
            st = self.transport.state(self.host) or {}
        except Exception:
            return None
        if not st:
            return None
        with self._lock:
            cur = self._state
        mv = st.get("mv")
        pw = str(st.get("pw") or "")
        mu = st.get("mu")
        return cur.with_(
            connected=True,
            volume_db=(float(mv) - 80.0) if isinstance(mv, (int, float)) else cur.volume_db,
            muted=mu if isinstance(mu, bool) else cur.muted,
            powered_on=True if pw == "ON" else False if pw == "STANDBY" else cur.powered_on,
        )

    def _monitor_loop(self, gen: int) -> None:
        last_full = time.monotonic()
        while self._active and gen == self._generation:
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            if not (self._active and gen == self._generation):
                return
            if time.monotonic() - last_full >= self._full_poll_s:
                last_full = time.monotonic()
                self.get_state()
                continue
            new = self._from_hub()
            if new is not None and self._reachable:
                self._merge(new, source="hub")

    def _merge(self, new: ReceiverState, *, source: str) -> None:
        with self._lock:
            old, self._state = self._state, new
            listeners = list(self._listeners)
        if new == old:
            return
        changes = old.diff(new)
        if old.connected != new.connected:
            self._emit(EVT_CONNECTION, "receiver reachable" if new.connected else "receiver unreachable")
        field_changes = {k: v for k, v in changes.items() if k != "connected"}
        if field_changes and new.connected and old.connected:
            own = time.monotonic() - self._last_cmd_mono < _OWN_COMMAND_WINDOW_S
            desc = ", ".join(f"{k}: {a!r} → {b!r}" for k, (a, b) in field_changes.items())
            self._emit(EVT_RESPONSE if own else EVT_EXTERNAL, f"[{source}] {desc}")
        for cb in listeners:
            try:
                cb(old, new)
            except Exception:
                pass

    def _emit(self, kind: str, text: str) -> None:
        sink = self._sink
        if sink is not None:
            try:
                sink(ReceiverEvent(kind, text))
            except Exception:
                pass
