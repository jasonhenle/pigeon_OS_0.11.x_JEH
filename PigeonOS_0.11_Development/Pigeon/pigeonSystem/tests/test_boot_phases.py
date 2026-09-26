"""The ``bootstrap()`` phases in ``pigeon.core.boot`` must stay wired correctly.

Each phase reads its inputs as ``x = ctx.x`` at the top of ``run`` and writes
back what later phases need as ``ctx.y = y``. These checks catch the edits
that would break that silently: reading a name nobody wrote yet, or a name
that is neither local, imported nor a builtin.
"""

import ast
import builtins
import glob
import os
import symtable
import unittest

_SYS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOOT = os.path.join(_SYS, "pigeon", "core", "boot")


def _phase_paths(prefix="p"):
    """``p*`` = bootstrap() phases, ``m*`` = main() setup phases."""
    return sorted(glob.glob(os.path.join(_BOOT, f"{prefix}[0-9][0-9]_*.py")))


def _all_phase_paths():
    return _phase_paths("m") + _phase_paths("p")


def _run_fn(tree):
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")


def _is_ctx_attr(node, store):
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "ctx" and isinstance(node.ctx, ast.Store if store else ast.Load))


def _prologue_reads(fn):
    """Names read by the leading ``x = ctx.x`` lines (plain or ``try``-guarded)."""
    out = []
    for stmt in fn.body:
        inner = stmt
        if isinstance(stmt, ast.Try) and len(stmt.body) == 1 and len(stmt.handlers) == 1:
            inner = stmt.body[0]
        if isinstance(inner, ast.Assign) and _is_ctx_attr(inner.value, store=False):
            out.append(inner.value.attr)
        else:
            break
    return out


def _seed_names(var):
    """Keyword names of the ``<var> = _BootContext(...)`` seed in pigeon_0_9.py."""
    src = open(os.path.join(_SYS, "pigeon_0_9.py"), encoding="utf-8").read()
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == var
                and isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "_BootContext"):
            return {k.arg for k in n.value.keywords}
    raise AssertionError(f"no {var} = _BootContext(...) in pigeon_0_9.py")


class BootPhaseWiringTests(unittest.TestCase):
    def test_phase_order_matches_callers(self):
        src = open(os.path.join(_SYS, "pigeon_0_9.py"), encoding="utf-8").read()
        for prefix, arg in (("m", "_main_ctx"), ("p", "ctx")):
            mods = [os.path.basename(p)[:-3] for p in _phase_paths(prefix)]
            calls = [ln.strip() for ln in src.splitlines()
                     if ln.strip().startswith(f"_boot_{prefix}") and ln.strip().endswith(f".run({arg})")]
            self.assertEqual(calls, [f"_boot_{m}.run({arg})" for m in mods])

    def test_prologue_reads_only_names_already_written(self):
        for prefix, var in (("m", "_main_ctx"), ("p", "ctx")):
            written = _seed_names(var)
            for path in _phase_paths(prefix):
                fn = _run_fn(ast.parse(open(path, encoding="utf-8").read()))
                for name in _prologue_reads(fn):
                    self.assertIn(name, written, f"{os.path.basename(path)} reads ctx.{name} before any phase wrote it")
                for node in ast.walk(fn):
                    if isinstance(node, ast.Assign):
                        for t in node.targets:
                            if _is_ctx_attr(t, store=True):
                                written.add(t.attr)

    def test_phases_only_read_bound_names(self):
        for path in _all_phase_paths():
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
            known = set(dir(builtins))
            for stmt in tree.body:
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    known |= {(a.asname or a.name).split(".")[0] for a in stmt.names}
                elif isinstance(stmt, ast.FunctionDef):
                    known.add(stmt.name)
            table = symtable.symtable(src, path, "exec")

            def walk(t):
                yield t
                for c in t.get_children():
                    yield from walk(c)

            for t in walk(table):
                for sym in t.get_symbols():
                    if sym.is_global() and sym.is_referenced():
                        self.assertIn(sym.get_name(), known, f"{os.path.basename(path)}: {sym.get_name()}")

    def test_missing_name_raises_name_error(self):
        from pigeon.core.boot.context import BootContext

        ctx = BootContext(a=1)
        self.assertEqual(ctx.a, 1)
        with self.assertRaises(NameError):
            ctx.not_bound_yet


if __name__ == "__main__":
    unittest.main()
