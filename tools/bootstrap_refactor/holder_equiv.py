"""Check that a holderize.py run was a pure rename.

Usage::

    python3 holder_equiv.py BEFORE.py AFTER.py NAME [NAME ...]

Reads ``NAME[0]`` in AFTER back as ``NAME``, unwraps the ``[init]`` /
``list[T]`` of the initial binding, drops NAME from ``nonlocal`` statements
in both files, and requires the two ASTs to be identical. Also lists any bare
``NAME`` left in AFTER other than its initial binding (each must be a local
that shadows bootstrap()'s variable). Exit 1 on any difference.
"""
import ast
import sys

before, after, *specs = sys.argv[1:]
REN = {}
for sp in specs:
    old, _, new = sp.partition(":")
    REN[new or old] = old
V = set(REN)


class Undo(ast.NodeTransformer):
    def visit_Subscript(self, n):
        n = self.generic_visit(n)
        if (isinstance(n.value, ast.Name) and n.value.id in V
                and isinstance(n.slice, ast.Constant) and n.slice.value == 0):
            return ast.Name(id=REN[n.value.id], ctx=n.ctx)
        return n

    def visit_Assign(self, n):
        n = self.generic_visit(n)
        if (len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in V
                and isinstance(n.value, ast.List) and len(n.value.elts) == 1):
            n.value = n.value.elts[0]
            n.targets[0] = ast.Name(id=REN[n.targets[0].id], ctx=ast.Store())
        return n

    def visit_AnnAssign(self, n):
        n = self.generic_visit(n)
        if (isinstance(n.target, ast.Name) and n.target.id in V
                and isinstance(n.value, ast.List) and len(n.value.elts) == 1):
            n.value = n.value.elts[0]
            n.annotation = n.annotation.slice
            n.target = ast.Name(id=REN[n.target.id], ctx=ast.Store())
        return n


class DropNonlocal(ast.NodeTransformer):
    def generic_visit(self, n):
        super().generic_visit(n)
        for f in ("body", "orelse", "finalbody"):
            b = getattr(n, f, None)
            if isinstance(b, list):
                out = []
                for st in b:
                    if isinstance(st, ast.Nonlocal):
                        st.names = [x for x in st.names if x not in V and x not in REN.values()]
                        if not st.names:
                            continue
                    out.append(st)
                setattr(n, f, out)
        return n


src_after = open(after).read()
a_tree = ast.parse(src_after)
subscripted = {id(n.value) for n in ast.walk(a_tree)
               if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)}
bare = [(n.id, n.lineno) for n in ast.walk(a_tree)
        if isinstance(n, ast.Name) and n.id in V and id(n) not in subscripted]
o = DropNonlocal().visit(ast.parse(open(before).read()))
c = DropNonlocal().visit(Undo().visit(a_tree))
same = ast.dump(o) == ast.dump(c)
print("AST:", "IDENTICAL" if same else "DIFFERENT")
print("bare references left (should be initial bindings / shadowing locals only):")
for name, line in sorted(bare, key=lambda x: x[1]):
    print(f"  {name} line {line}")
sys.exit(0 if same else 1)
