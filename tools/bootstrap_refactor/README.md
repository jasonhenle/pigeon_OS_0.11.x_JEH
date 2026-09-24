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
