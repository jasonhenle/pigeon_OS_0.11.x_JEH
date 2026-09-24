import ast
def names_bound(stmts):
    """Names definitely bound after executing stmts (module-level, simple flow)."""
    out = set()
    for s in stmts:
        out |= stmt_bound(s)
    return out
def targets(t):
    if isinstance(t, ast.Name): return {t.id}
    if isinstance(t, (ast.Tuple, ast.List)):
        r=set()
        for e in t.elts: r|=targets(e)
        return r
    if isinstance(t, ast.Starred): return targets(t.value)
    return set()
def stmt_bound(s):
    if isinstance(s, ast.Assign):
        r=set()
        for t in s.targets: r|=targets(t)
        return r
    if isinstance(s, (ast.AnnAssign, ast.AugAssign)): return targets(s.target) if getattr(s,'value',1) is not None else set()
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): return {s.name}
    if isinstance(s, ast.Import): return {(a.asname or a.name).split('.')[0] for a in s.names}
    if isinstance(s, ast.ImportFrom): return {a.asname or a.name for a in s.names}
    if isinstance(s, ast.If):
        return names_bound(s.body) & names_bound(s.orelse)
    if isinstance(s, ast.Try):
        body = names_bound(s.body) | names_bound(s.orelse)
        # exception may fire anywhere in body: a handler path must bind it itself
        hs = [names_bound(h.body) for h in s.handlers]
        common = set.intersection(*hs) if hs else body
        return (body & common) | names_bound(s.finalbody)
    if isinstance(s, ast.With): return names_bound(s.body)
    return set()
