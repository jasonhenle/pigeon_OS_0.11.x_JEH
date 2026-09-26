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


def _phase_paths():
    return sorted(glob.glob(os.path.join(_BOOT, "p[0-9][0-9]_*.py")))


def _run_fn(tree):
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")


def _is_ctx_attr(node, store):
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "ctx" and isinstance(node.ctx, ast.Store if store else ast.Load))


class BootPhaseWiringTests(unittest.TestCase):
    def test_phase_order_matches_bootstrap(self):
        src = open(os.path.join(_SYS, "pigeon_0_9.py"), encoding="utf-8").read()
        mods = [os.path.basename(p)[:-3] for p in _phase_paths()]
        calls = [ln.strip() for ln in src.splitlines() if ln.strip().startswith("_boot_p") and ln.strip().endswith(".run(ctx)")]
        self.assertEqual(calls, [f"_boot_{m}.run(ctx)" for m in mods])

    def test_prologue_reads_only_names_already_written(self):
        src = open(os.path.join(_SYS, "pigeon_0_9.py"), encoding="utf-8").read()
        seed_call = next(n for n in ast.walk(ast.parse(src))
                         if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_BootContext")
        written = {k.arg for k in seed_call.keywords}
        for path in _phase_paths():
            fn = _run_fn(ast.parse(open(path, encoding="utf-8").read()))
            for stmt in fn.body:
                if not (isinstance(stmt, ast.Assign) and _is_ctx_attr(stmt.value, store=False)):
                    break
                self.assertIn(stmt.value.attr, written, f"{os.path.basename(path)} reads ctx.{stmt.value.attr} before any phase wrote it")
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if _is_ctx_attr(t, store=True):
                            written.add(t.attr)

    def test_phases_only_read_bound_names(self):
        for path in _phase_paths():
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
