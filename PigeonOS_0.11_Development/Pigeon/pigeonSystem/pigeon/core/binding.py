"""Bind extracted ``bootstrap()`` helpers to the app state they depend on."""

from __future__ import annotations

import functools


def bind_deps(fn, /, **deps):
    """Return ``fn`` with its keyword-only dependencies pre-filled.

    Helpers moved out of ``bootstrap()`` take the state they used to close over
    as keyword-only arguments. ``bootstrap()`` calls this once where the old
    nested ``def`` stood, so every existing caller (direct calls, Tk callbacks,
    ``root.after``) keeps working unchanged. Name and docstring are preserved
    for tracebacks and logging.
    """
    bound = functools.partial(fn, **deps)
    functools.update_wrapper(bound, fn)
    return bound
