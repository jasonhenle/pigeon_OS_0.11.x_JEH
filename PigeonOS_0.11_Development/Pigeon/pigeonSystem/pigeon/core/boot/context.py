"""Shared state for the ``bootstrap()`` phases in ``pigeon.core.boot``."""

from __future__ import annotations


class BootContext:
    """One attribute per former ``bootstrap()`` local (plus the ``main()`` locals
    and module globals the phases read, seeded up front).

    Phases read their inputs from it when they start and write back what later
    phases need when they end. Reading a name nobody has bound yet raises
    ``NameError`` -- what the original closure lookup raised.
    """

    def __init__(self, **names: object) -> None:
        self.__dict__.update(names)

    def __getattr__(self, name: str) -> object:
        raise NameError(f"name {name!r} is not defined (not bound yet in bootstrap)")
