import ast, io, json, os, sys, tokenize, inspect, re, copy
sys.path.insert(0, os.path.expanduser("~/an"))
ROOT = sys.argv[1]  # pigeonSystem dir
PLAN = json.load(open(sys.argv[2]))  # {module: [names]}
ROWS = {r["name"]: r for r in json.load(open(sys.argv[3]))}
NEW_DOCS = json.load(open(sys.argv[4]))  # {module: first-line doc} for new modules
path = os.path.join(ROOT, "pigeon_0_9.py")
src = open(path).read()
lines = src.splitlines(keepends=True)
tree = ast.parse(src)
main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
boot = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == "bootstrap")
defs = {s.name: s for s in boot.body if isinstance(s, ast.FunctionDef)}

# module-level import statements usable directly
direct = {}
for s in tree.body:
    if isinstance(s, ast.Import):
        for a in s.names:
            if a.asname or "." not in a.name:
                direct[a.asname or a.name] = f"import {a.name}" + (f" as {a.asname}" if a.asname else "")
    elif isinstance(s, ast.ImportFrom) and s.level == 0:
        for a in s.names:
            direct[a.asname or a.name] = f"from {s.module} import {a.name}" + (f" as {a.asname}" if a.asname else "")

IND = " " * 8

def add_kwonly(text, fn, deps):
    """Insert keyword-only params before the def's closing paren."""
    if not deps:
        return text
    toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    depth = 0; start = None; close = None; last_sig = None
    for tk in toks:
        if tk.type == tokenize.OP and tk.string == "(":
            depth += 1
            if depth == 1 and start is None: start = tk
            continue
        if tk.type == tokenize.OP and tk.string == ")":
            depth -= 1
            if depth == 0: close = tk; break
        if depth >= 1 and tk.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
            last_sig = tk
    a = fn.args
    has_params = bool(a.posonlyargs or a.args or a.vararg or a.kwonlyargs)
    star_present = bool(a.vararg or a.kwonlyargs)
    trailing_comma = last_sig is not None and last_sig.string == "," and has_params
    ins = ", ".join(deps)
    if not has_params:
        ins = "*, " + ins
    elif not star_present:
        ins = ("" if trailing_comma else ", ") + "*, " + ins
    else:
        ins = ("" if trailing_comma else ", ") + ins
    tl = text.splitlines(keepends=True)
    r, c = close.start
    line = tl[r - 1]
    if trailing_comma and "\n" in "".join(tl[last_sig.start[0]-1:r-1]) and last_sig.start[0] != r:
        # multi-line param list ending ',\n    )' : add a new line before ')'
        indent = re.match(r"\s*", tl[last_sig.start[0]-1]).group(0)
        tl.insert(r - 1, indent + ins + ",\n")
        return "".join(tl)
    tl[r - 1] = line[:c] + ins + line[c:]
    return "".join(tl)

def norm(fn):
    fn = copy.deepcopy(fn)
    if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None), ast.Constant) and isinstance(fn.body[0].value.value, str):
        fn.body[0].value.value = inspect.cleandoc(fn.body[0].value.value)
    for n in ast.walk(fn):
        for k in ("lineno", "col_offset", "end_lineno", "end_col_offset"):
            if hasattr(n, k): setattr(n, k, 0)
    return ast.dump(fn, include_attributes=False)

modules_out = {}
replacements = []  # (start_line, end_line, new_text)
inserts = []  # (after_line, original_def_line, text)
report = []
for mod, names in PLAN.items():
    for name in names:
        r = ROWS[name]; fn = defs[name]
        assert r["safe"], name
        body = lines[fn.lineno - 1: fn.end_lineno]
        for l in body:
            assert not l.strip() or l.startswith(IND), (name, l)
        text = "".join(l[len(IND):] if l.strip() else l.lstrip(" ") for l in body)
        imps = [n for n in r["gdeps"] if n in direct]
        gdeps = [n for n in r["gdeps"] if n not in direct]
        deps = sorted(set(r["bdeps"]) | set(r["mdeps"]) | set(gdeps))
        new = add_kwonly(text, fn, deps)
        nf = ast.parse(new).body[0]
        # verify: identical once the added kw-only params are removed
        chk = copy.deepcopy(nf)
        k = len(deps)
        chk.args.kwonlyargs = chk.args.kwonlyargs[: len(chk.args.kwonlyargs) - k]
        chk.args.kw_defaults = chk.args.kw_defaults[: len(chk.args.kw_defaults) - k]
        assert norm(chk) == norm(fn), f"AST mismatch {name}"
        modules_out.setdefault(mod, []).append((name, new, set(r["imps"]) | set(imps)))
        if deps:
            b = f"{IND}{name} = _bind_deps(\n{IND}    _core_{mod}.{name},\n" + "".join(f"{IND}    {d}={d},\n" for d in deps) + f"{IND})\n"
            one = f"{IND}{name} = _bind_deps(_core_{mod}.{name}, " + ", ".join(f"{d}={d}" for d in deps) + ")\n"
            if len(one) <= 100: b = one
        else:
            b = f"{IND}{name} = _core_{mod}.{name}\n"
        if r.get("bind_after") is None:
            replacements.append((fn.lineno, fn.end_lineno, b))
        else:
            # pass 3: bind later, after the last forward dep; comments above the def move too
            start = fn.lineno
            while start > 1 and lines[start - 2].strip().startswith("#") and lines[start - 2].startswith(IND) and not lines[start - 2].startswith(IND + " "):
                start -= 1
            moved = "".join(lines[start - 1: fn.lineno - 1])
            replacements.append((start, fn.end_lineno, ""))
            after = boot.body[r["bind_after"]].end_lineno
            inserts.append((after, fn.lineno, "\n" + moved + b))
        report.append((mod, name, fn.end_lineno - fn.lineno + 1, len(deps)))

# apply edits on original line numbers
drop = {}
for s_, e_, b in replacements:
    drop[s_] = (e_, b)
ins = {}
for after, order, text in sorted(inserts):
    ins.setdefault(after, []).append(text)
out, k = [], 1
while k <= len(lines):
    if k in drop:
        e_, b = drop[k]
        if b: out.append(b)
        elif out and not out[-1].strip() and e_ < len(lines) and not lines[e_].strip():
            k_end = e_ + 1  # drop one of the two blank lines around a removed def
            e_ = k_end
        for a in range(k, e_ + 1):
            assert a not in ins, "insert inside a removed range"
        k = e_ + 1
        continue
    out.append(lines[k - 1])
    for text in ins.get(k, []):
        out.append(text)
    if k in ins and k < len(lines) and lines[k].strip():
        out.append("\n")
    k += 1
src2 = "".join(out)
# add module imports
for mod in PLAN:
    imp = f"from pigeon.core import {mod} as _core_{mod}\n"
    if imp not in src2:
        anchor = "from pigeon.core import device_control as _core_device_control\n"
        src2 = src2.replace(anchor, anchor + imp, 1)
open(path, "w").write(src2)

# write modules
for mod, fns in modules_out.items():
    mp = os.path.join(ROOT, "pigeon", "core", mod + ".py")
    needed = set()
    for _n, _t, imps in fns:
        for i in imps: needed.add(direct[i])
    if os.path.exists(mp):
        cur = open(mp).read()
        existing_names = set(re.findall(r"^def (\w+)", cur, re.M))
        for n, _t, _i in fns: assert n not in existing_names, (mod, n)
        m = ast.parse(cur)
        have = set(ast.get_source_segment(cur, s) for s in m.body if isinstance(s, (ast.Import, ast.ImportFrom)))
        add = sorted(x for x in needed if x not in have)
        last_imp = max(s.end_lineno for s in m.body if isinstance(s, (ast.Import, ast.ImportFrom)))
        cl = cur.splitlines(keepends=True)
        cl[last_imp:last_imp] = [a + "\n" for a in add]
        cur = "".join(cl).rstrip("\n") + "\n"
    else:
        cur = f'"""{NEW_DOCS[mod]}\n\nExtracted verbatim from ``bootstrap()`` in ``pigeon_0_9.py``. Each function\ntakes the app state it used to close over as keyword-only arguments;\n``bootstrap()`` binds them once with ``bind_deps`` so call sites are unchanged.\n"""\n\nfrom __future__ import annotations\n\n'
        cur += "".join(sorted(x + "\n" for x in needed))
    for n, t, _i in fns:
        cur += "\n\n" + t.rstrip("\n") + "\n"
    open(mp, "w").write(cur)
for r in report: print(*r)
print("moved", len(report), "functions,", sum(r[2] for r in report), "lines")
