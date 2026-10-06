"""Generic receiver interface and adapters (Denon, Fake).

Add a brand by writing an adapter that satisfies ``Receiver`` and registering
a factory in ``ADAPTERS``; no Pigeon or diagnostic-tool code changes.
"""

from __future__ import annotations

from collections.abc import Callable

from pigeon.receiver.base import (
    EVENT_KINDS,
    InputSelectable,
    Receiver,
    ReceiverEvent,
    ReceiverState,
)


def _denon(host: str) -> Receiver:
    from pigeon.receiver.denon import DenonReceiver

    return DenonReceiver(host)


def _fake(host: str) -> Receiver:
    from pigeon.receiver.fake import FakeReceiver

    r = FakeReceiver()
    r.start_auto_pump()
    return r


# brand → factory(host). Factories import lazily so unused adapters cost nothing.
ADAPTERS: dict[str, Callable[[str], Receiver]] = {"denon": _denon, "fake": _fake}


def create_receiver(brand: str, host: str = "") -> Receiver:
    try:
        return ADAPTERS[brand.strip().lower()](host)
    except KeyError:
        raise ValueError(f"unknown receiver brand {brand!r}; known: {sorted(ADAPTERS)}") from None


__all__ = [
    "ADAPTERS", "EVENT_KINDS", "InputSelectable", "Receiver", "ReceiverEvent",
    "ReceiverState", "create_receiver",
]
