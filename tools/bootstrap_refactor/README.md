# bootstrap() lift tooling (passes 2–3)

Dev-only scripts used to move nested helpers out of `bootstrap()` in
`pigeonSystem/pigeon_0_9.py` into `pigeon/core/`. Not shipped.

Run from `PigeonOS_0.11_Development/Pigeon/pigeonSystem` with Python 3.10+:

```bash
T=../../../tools/bootstrap_refactor
python3 $T/pass2.py pigeon_0_9.py /tmp/p2.json        # classify every remaining helper
python3 $T/transform.py . $T/plan.json /tmp/p2.json $T/new_module_docs.json
```

A helper is lifted only when all of these hold:

- no `nonlocal` / `global`, no decorators, no `**kwargs`, constant defaults only, no class defs;
- every name it closes over from `bootstrap()` is bound exactly once, unconditionally,
  at top level, before the helper's `def`, and is never written via `nonlocal`;
- every name it closes over from `main()` is bound the same way before `root.mainloop()`;
- every module global it reads is definitely bound at import and never rebound with `global`.

Module-level `import x` / `from x import y` names are imported directly in the core
module; everything else becomes a keyword-only parameter bound with `bind_deps` at the
old `def` site. `transform.py` asserts each moved function's AST equals the original
apart from the added keyword-only params (docstrings compared after `cleandoc`).

`plan.json` maps target module -> helper names; it must list exactly the helpers that
`pass2.py` marks safe.

## Pass 3: forward references

Helpers blocked only because they close over something bound *later* in `bootstrap()`
can be lifted if their `bind_deps()` call moves down to just after the last of those
names is bound. `pass3.py` checks that nothing can use the helper while its name is
unbound (scope-aware call graph, Tk registrations, event-loop pumps, running threads,
callbacks handed off early); see its docstring.

```bash
python3 $T/pass2.py pigeon_0_9.py /tmp/p2.json
python3 $T/pass3.py pigeon_0_9.py /tmp/p2.json /tmp/p3.json
python3 $T/transform.py . $T/plan3.json /tmp/p3.json $T/new_module_docs.json
```

## Checks after any pass

```bash
python3 $T/verify_order.py pigeon_0_9.py      # bootstrap() never reads a name before binding it
python3 -m pyflakes pigeon/core/*.py
HOME=$(mktemp -d) python3 $T/smoke_bootstrap.py   # runs main() + bootstrap() top level with a fake tkinter
```

`smoke_bootstrap.py` needs the desktop requirements (no display or real Tk). It should
end with `SMOKE: bootstrap returned` / `SMOKE: main returned 0`.

## Pass 4: holders for rebound state

Most remaining helpers were blocked because they share variables that
`bootstrap()` reassigns (via `nonlocal` or a second top-level binding).
`holderize.py` converts such a variable into a one-element holder list — the
same `x_holder[0]` pattern the codebase already uses — so the name is bound
once and helpers can take it as a dependency:

```bash
python3 $T/holderize.py pigeon_0_9.py skip_cache dev_phase active_tmdb_title_key active_tmdb_display_title
```

It rewrites `x` → `x[0]` only where the name resolves to `bootstrap()`'s
binding (scope-aware; shadowing locals are left alone), wraps the first
top-level binding in `[...]`, and drops `x` from `nonlocal` declarations.
Variables that were only bound twice by `x = None` / `if ...: x = Widget()`
were instead collapsed to a single conditional expression by hand
(`status_bar_widget`, `view_circles_widget`, `main_settings_widget`).
Then re-run pass 2 / pass 3 and `transform.py` with `plan4.json` / `plan4b.json`.

Known pre-existing quirk preserved on purpose: `_prefetch_pigeon_update_badge`,
`_remove_streaming_device_at` and `_remove_receiver_device_at` assign
`skip_cache = None` without `nonlocal`, so they never cleared the render cache.

## Pass 5

`skip_cache` was still treated as rebound because three functions assign a
*local* `skip_cache` (the quirk above), and one nested `nonlocal skip_cache`
pointed at such a local. Those locals are renamed `_stale_skip_cache`
(behaviour unchanged). Then `playback_overlay_widget` was bound once and
`command_entry_visible`, `scene_enabled`, `info_cluster_blits`,
`last_timecode_motion_mono` holderized; `plan5.json` / `plan5b.json` lift 24 helpers.

Check every holderize run is a pure rename:

```bash
cp pigeon_0_9.py /tmp/before.py
python3 $T/holderize.py pigeon_0_9.py NAME ...
python3 $T/holder_equiv.py /tmp/before.py pigeon_0_9.py NAME ...   # AST: IDENTICAL
```

Dead code noticed (never called, kept verbatim): `_remove_saved_receiver_device`,
`set_current_receiver_only`.
