"""Pass-2 candidate analysis for lifting bootstrap() helpers."""
import ast, symtable, sys, collections, json, builtins
SRC = sys.argv[1]
src = open(SRC).read()
tree = ast.parse(src)
main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
boot = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == "bootstrap")
st = symtable.symtable(src, SRC, "exec")
mt = next(c for c in st.get_children() if c.get_name() == "main")
bt = next(c for c in mt.get_children() if c.get_name() == "bootstrap")
BUILTINS = set(dir(builtins))

def scope_binds(fn):
    binds = collections.defaultdict(list)
    class B(ast.NodeVisitor):
        top=None; depth=0
        def add(s, name, node): binds[name].append((node.lineno, s.top if s.depth == 0 else None))
        def visit_FunctionDef(s, n):
            for d in n.decorator_list + n.args.defaults + [d for d in n.args.kw_defaults if d]: s.visit(d)
            s.add(n.name, n)
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_ClassDef(s, n): s.add(n.name, n)
        def visit_Lambda(s, n): pass
        def visit_ListComp(s, n): pass
        visit_SetComp = visit_DictComp = visit_GeneratorExp = visit_ListComp
        def visit_Name(s, n):
            if isinstance(n.ctx, (ast.Store, ast.Del)): s.add(n.id, n)
        def visit_NamedExpr(s, n): s.add(n.target.id, n); s.visit(n.value)
        def visit_AugAssign(s, n):
            if isinstance(n.target, ast.Name): s.add(n.target.id, n); s.add(n.target.id, n)
            else: s.visit(n.target)
            s.visit(n.value)
        def visit_Import(s, n):
            for a in n.names: s.add((a.asname or a.name).split(".")[0], n)
        visit_ImportFrom = visit_Import
        def visit_ExceptHandler(s, n):
            if n.name: s.add(n.name, n)
            s.generic_visit(n)
    for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs + [x for x in (fn.args.vararg, fn.args.kwarg) if x]:
        binds[a.arg].append((fn.lineno, -1))
    v = B()
    for i, stmt in enumerate(fn.body):
        v.top = i
        v.depth = 0 if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom, ast.Return, ast.Pass, ast.Delete, ast.Nonlocal, ast.Global, ast.Raise, ast.Assert)) else 1
        v.visit(stmt)
    return binds

nl_main = collections.Counter()
for n in ast.walk(main):
    if isinstance(n, ast.Nonlocal):
        for nm in n.names: nl_main[nm] += 1
gl_names = set()
for n in ast.walk(tree):
    if isinstance(n, ast.Global): gl_names.update(n.names)

bb = scope_binds(boot)
mb = scope_binds(main)
boot_idx_in_main = main.body.index(boot)
# cutoff: main top-level statement that starts mainloop
cut = next(i for i, s in enumerate(main.body) if isinstance(s, ast.Try) and "mainloop" in ast.unparse(s))

def boot_stable(name, before):
    b = bb.get(name, [])
    return len(b) == 1 and b[0][1] is not None and b[0][1] >= 0 and b[0][1] < before and nl_main[name] == 0
def main_stable(name):
    b = mb.get(name, [])
    return len(b) == 1 and b[0][1] is not None and b[0][1] >= 0 and b[0][1] < cut and nl_main[name] == 0

# module-level: plain unconditional `import x [as y]`
plain_imports = {}
mod_binds = collections.Counter()
for s in tree.body:
    if isinstance(s, ast.Import):
        for a in s.names:
            if a.asname or "." not in a.name:
                plain_imports[a.asname or a.name] = f"import {a.name}" + (f" as {a.asname}" if a.asname else "")
for n in ast.walk(tree):
    pass

def tables_under(t):
    yield t
    for c in t.get_children(): yield from tables_under(c)

def const_default(d):
    try:
        ast.literal_eval(d); return True
    except Exception:
        return False

top_defs = [(i, s) for i, s in enumerate(boot.body) if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
rows = []
reasons = collections.Counter()
by_key = {}
for c in bt.get_children():
    by_key.setdefault(c.get_name(), []).append(c)
for i, f in top_defs:
    cands = [c for c in by_key.get(f.name, []) if c.get_lineno() == f.lineno]
    t = cands[0]
    r = []
    if f.decorator_list: r.append("decorated")
    if isinstance(f, ast.AsyncFunctionDef): r.append("async")
    if f.args.kwarg: r.append("**kwargs")
    if not all(const_default(d) for d in f.args.defaults + [d for d in f.args.kw_defaults if d]): r.append("nonconst-default")
    if any(isinstance(n, (ast.Nonlocal, ast.Global)) for n in ast.walk(f)): r.append("nonlocal")
    if any(isinstance(n, ast.ClassDef) for n in ast.walk(f)): r.append("classdef")
    frees = set(t.get_frees())
    bdeps, mdeps, gdeps, imps = [], [], [], set()
    for n in sorted(frees):
        if bt.lookup(n).is_local():
            if boot_stable(n, i): bdeps.append(n)
            elif n == f.name: r.append("recursive")
            else:
                b = bb.get(n, [])
                r.append("forward:" + n if (len(b) == 1 and b[0][1] is not None and b[0][1] > i and nl_main[n] == 0) else "rebound:" + n)
        elif mt.lookup(n).is_local():
            if main_stable(n): mdeps.append(n)
            else: r.append("main-rebound:" + n)
        else:
            r.append("??:" + n)
    for tb in tables_under(t):
        for sym in tb.get_symbols():
            if sym.is_global() and sym.is_referenced() and not sym.is_declared_global():
                n = sym.get_name()
                if n in ("__file__", "__name__", "__spec__", "__doc__"): r.append("dunder")
                elif n in gl_names: r.append("global-rebound:" + n)
                elif n in plain_imports: imps.add(n)
                elif n in BUILTINS and n not in {k for k in dir(tree)}:
                    # builtin unless module shadows it
                    pass
                else: gdeps.append(n)
    gdeps = sorted(set(gdeps))
    safe = not r
    kind = "SAFE" if safe else "blocked"
    reasons[kind] += 1
    rows.append(dict(name=f.name, line=f.lineno, end=f.end_lineno, idx=i, bdeps=bdeps, mdeps=mdeps, gdeps=gdeps, imps=sorted(imps), reasons=r, safe=safe))
json.dump(rows, open(sys.argv[2], "w"), indent=1)
print(reasons)
fw = collections.Counter(x for r_ in rows for x in r_["reasons"] if x.startswith("forward"))
print("only-forward-blocked:", sum(1 for r_ in rows if r_["reasons"] and all(x.startswith("forward") for x in r_["reasons"])))
