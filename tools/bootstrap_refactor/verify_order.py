"""Every name bootstrap() reads while executing its top level must already be bound.

Reads inside nested def/lambda bodies are skipped (they run later). Prints
violations; exit 1 if any. Run before and after a transform and compare.
"""
import ast, sys
src = open(sys.argv[1]).read()
t = ast.parse(src)
main = next(n for n in t.body if isinstance(n, ast.FunctionDef) and n.name == "main")
boot = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == "bootstrap")
import symtable
st = symtable.symtable(src, sys.argv[1], "exec")
mt = next(c for c in st.get_children() if c.get_name() == "main")
bt = next(c for c in mt.get_children() if c.get_name() == "bootstrap")
local = {s.get_name() for s in bt.get_symbols() if s.is_local()}
first_bind = {}
for i, s in enumerate(boot.body):
    for n in ast.walk(s):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store): first_bind.setdefault(n.id, i)
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n in boot.body: first_bind.setdefault(n.name, i)
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names: first_bind.setdefault((a.asname or a.name).split(".")[0], i)
        if isinstance(n, ast.ExceptHandler) and n.name: first_bind.setdefault(n.name, i)
    if isinstance(s, (ast.FunctionDef, ast.ClassDef)): first_bind.setdefault(s.name, i)
bad = []
for i, s in enumerate(boot.body):
    stack = [s]
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(n.decorator_list + n.args.defaults + [d for d in n.args.kw_defaults if d]); continue
        if isinstance(n, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) and n is not s:
            # comprehension/lambda scopes: comprehension runs now; lambda later
            if isinstance(n, ast.Lambda): continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in local:
            if first_bind.get(n.id, 10**9) > i:
                bad.append((n.lineno, n.id))
        stack.extend(ast.iter_child_nodes(n))
for b in sorted(set(bad)): print("read-before-bind", *b)
print(len(set(bad)), "violations")
