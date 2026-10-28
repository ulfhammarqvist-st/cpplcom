"""LCOM4 for C++: how many unrelated jobs each class does. See README.md.

A class's methods are the nodes of a graph. Two methods are joined when both use the same field of the class, or
when one calls the other. LCOM4 is the number of connected parts: 1 is a cohesive class, 3 is three classes sharing
a name.

Methods are the class's own non-static member functions with a body, wherever that body is defined. Constructors,
destructors, copy/move assignment and defaulted or deleted members are left out: they touch every field by design
and would join everything. Fields are the class's own non-static data members; inherited fields join nothing. A call
into a base class joins the caller to the class's own overrides of whatever virtuals that base method reaches.

Sources are parsed with the libclang of the compiler in the compile database, so they parse exactly as they build.

A class's jobs are its LCOM4 minus its BARE parts: a lone method that touches no field, or only reads fields no
other method touches (constructor state). The report fails when a class has more jobs than --threshold.

Usage:
    python -m cpplcom --compile-commands <build>/compile_commands.json --root src [--exclude GLOB ...]
                      [--json out.json] [--threshold 1]
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import fnmatch
import functools
import json
import os
import pathlib
import shlex
import subprocess
import sys
from ctypes import POINTER, byref, c_uint

import clang.cindex as ci

METHOD_KINDS = {ci.CursorKind.CXX_METHOD, ci.CursorKind.FUNCTION_TEMPLATE, ci.CursorKind.CONVERSION_FUNCTION}
CLASS_KINDS = {ci.CursorKind.CLASS_DECL, ci.CursorKind.STRUCT_DECL, ci.CursorKind.CLASS_TEMPLATE,
               ci.CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION}
SCOPE_KINDS = CLASS_KINDS | {ci.CursorKind.NAMESPACE, ci.CursorKind.LINKAGE_SPEC, ci.CursorKind.UNEXPOSED_DECL}
DEFAULT_EXCLUDES = ("*/ext/*", "*/vendor/*", "*/third_party/*")
DROPPED_FLAGS = {"-c"}
DROPPED_FLAGS_WITH_VALUE = {"-o", "-MF", "-MT", "-MQ"}


def libclang_for(compiler: str) -> pathlib.Path:
    resource_dir = subprocess.run([compiler, "-print-resource-dir"], capture_output=True, text=True, check=True)
    lib_dir = pathlib.Path(resource_dir.stdout.strip()).parents[1]
    for pattern in ("libclang.dylib", "libclang.so", "libclang-*.so*", "libclang.so.*"):
        found = sorted(lib_dir.glob(pattern))
        if found:
            return found[0]
    sys.exit(f"no libclang next to {compiler} in {lib_dir}")


def system_includes(compiler: str, args: list[str]) -> list[str]:
    """libclang has no driver location to find the toolchain's libc++ from, so take the compiler's search list."""
    probe = subprocess.run([compiler, *args, "-x", "c++", "-E", "-v", os.devnull], capture_output=True, text=True)
    listing = probe.stderr.split("#include <...> search starts here:\n")[1].split("End of search list.")[0]
    flags = ["-nostdinc", "-Wno-unused-command-line-argument"]
    for line in listing.splitlines():
        path = line.strip()
        if path.endswith(" (framework directory)"):
            flags += ["-iframework", path.removesuffix(" (framework directory)")]
        else:
            flags += ["-isystem", path]
    return flags


def arguments_of(entry: dict) -> list[str]:
    argv = entry["arguments"] if "arguments" in entry else shlex.split(entry["command"])
    out, skip = [], False
    for arg in argv[1:]:
        if skip:
            skip = False
        elif arg in DROPPED_FLAGS_WITH_VALUE:
            skip = True
        elif arg not in DROPPED_FLAGS and os.path.normpath(os.path.join(entry["directory"], arg)) != entry["file"]:
            out.append(arg)
    return out


def template_of(cursor: ci.Cursor) -> ci.Cursor:
    template = ci.conf.lib.clang_getSpecializedCursorTemplate(cursor)
    return template if template and not template.kind.is_invalid() else cursor


def owner_usr(cursor: ci.Cursor) -> str | None:
    parent = cursor.semantic_parent
    return parent.canonical.get_usr() if parent and parent.kind in CLASS_KINDS else None


def overridden_by(cursor: ci.Cursor) -> list[str]:
    lib = ci.conf.lib
    lib.clang_getOverriddenCursors.argtypes = [ci.Cursor, POINTER(POINTER(ci.Cursor)), POINTER(c_uint)]
    lib.clang_getOverriddenCursors.restype = None
    usrs, todo = [], [cursor]
    while todo:
        found, count = POINTER(ci.Cursor)(), c_uint()
        lib.clang_getOverriddenCursors(todo.pop(), byref(found), byref(count))
        for i in range(count.value):
            base = ci.Cursor.from_buffer_copy(found[i])
            base._tu = cursor._tu
            usrs.append(template_of(base).canonical.get_usr())
            todo.append(base)
        if count.value:
            lib.clang_disposeOverriddenCursors(found)
    return usrs


def bases_of(definition: ci.Cursor) -> list[str]:
    bases = []
    for child in definition.get_children():
        if child.kind == ci.CursorKind.CXX_BASE_SPECIFIER and (decl := child.referenced) is not None:
            bases.append(template_of(decl).canonical.get_usr())
    return bases


def self_parameter(cursor: ci.Cursor) -> ci.Cursor | None:
    """The `SELF& self` of a static member written as a method on an explicit object, as for static dispatch."""
    params = [c for c in cursor.get_children() if c.kind == ci.CursorKind.PARM_DECL]
    if not params or params[0].type.kind not in (ci.TypeKind.LVALUEREFERENCE, ci.TypeKind.RVALUEREFERENCE):
        return None
    pointee = params[0].type.get_pointee()
    own = {c.spelling for c in cursor.get_children() if c.kind == ci.CursorKind.TEMPLATE_TYPE_PARAMETER}
    declaration = pointee.get_declaration()
    is_class = declaration.kind in CLASS_KINDS and owner_usr(cursor) == template_of(declaration).canonical.get_usr()
    return params[0] if pointee.spelling.removeprefix("const ") in own or is_class else None


def is_method(cursor: ci.Cursor) -> bool:
    return not cursor.is_static_method() or self_parameter(cursor) is not None


def is_counted_method(cursor: ci.Cursor) -> bool:
    if cursor.kind not in METHOD_KINDS or not cursor.is_definition() or not is_method(cursor):
        return False
    if cursor.kind == ci.CursorKind.FUNCTION_TEMPLATE and cursor.semantic_parent.kind not in CLASS_KINDS:
        return False
    return not (cursor.is_default_method() or cursor.is_deleted_method()
                or cursor.is_copy_assignment_operator_method() or cursor.is_move_assignment_operator_method())


def referenced_decls(cursor: ci.Cursor):
    if cursor.kind == ci.CursorKind.OVERLOADED_DECL_REF:
        for i in range(ci.conf.lib.clang_getNumOverloadedDecls(cursor)):
            yield ci.conf.lib.clang_getOverloadedDecl(cursor, i)
    elif cursor.referenced is not None:
        yield cursor.referenced


TRANSPARENT_KINDS = {ci.CursorKind.UNEXPOSED_EXPR, ci.CursorKind.PAREN_EXPR}


def walk_with_parents(cursor: ci.Cursor, parents: tuple = ()):
    yield cursor, parents
    for child in cursor.get_children():
        yield from walk_with_parents(child, (cursor, *parents))


def only_read(parents: tuple) -> bool:
    """True when a field is used as the object of a const member call (implicit casts aside); anything else may write."""
    user = next((p for p in parents if p.kind not in TRANSPARENT_KINDS), None)
    return (user is not None and user.kind in (ci.CursorKind.MEMBER_REF_EXPR, ci.CursorKind.CALL_EXPR)
            and user.referenced is not None and user.referenced.kind in METHOD_KINDS
            and user.referenced.is_const_method())


def members_by_name(definition: ci.Cursor) -> dict[str, list[ci.Cursor]]:
    members = collections.defaultdict(list)
    for child in definition.get_children():
        if child.kind == ci.CursorKind.FIELD_DECL or (child.kind in METHOD_KINDS and is_method(child)):
            members[child.spelling].append(child)
    return members


def on_self(node: ci.Cursor, self_param: ci.Cursor | None, members: dict[str, list[ci.Cursor]], parents: tuple):
    """Members named through `self.` where SELF is a template parameter: dependent, so resolved by name and arity."""
    if self_param is None or node.kind != ci.CursorKind.MEMBER_REF_EXPR or node.referenced is not None:
        return []
    base = next(iter(node.get_children()), None)
    while base is not None and base.kind in TRANSPARENT_KINDS:
        base = next(iter(base.get_children()), None)
    if base is None or base.referenced is None or base.referenced != self_param:
        return []
    tokens = list(node.get_tokens())
    candidates = members.get(tokens[-1].spelling, []) if tokens else []
    call = next((p for p in parents if p.kind not in TRANSPARENT_KINDS), None)
    if call is not None and call.kind == ci.CursorKind.CALL_EXPR:
        arity = len(list(call.get_children())) - 1
        matching = [c for c in candidates
                    if len([p for p in c.get_children() if p.kind == ci.CursorKind.PARM_DECL]) == arity]
        candidates = matching or candidates
    return candidates


def uses_of(body: ci.Cursor, class_usr: str, members: dict[str, list[ci.Cursor]]) -> tuple[set[str], set[str], bool]:
    fields, calls, writes = set(), set(), False
    self_param = self_parameter(body) if body.is_static_method() else None
    for node, parents in walk_with_parents(body):
        if node.kind not in (ci.CursorKind.MEMBER_REF_EXPR, ci.CursorKind.DECL_REF_EXPR, ci.CursorKind.CALL_EXPR,
                             ci.CursorKind.OVERLOADED_DECL_REF, ci.CursorKind.MEMBER_REF):
            continue
        for decl in [*referenced_decls(node), *on_self(node, self_param, members, parents)]:
            decl = template_of(decl)
            if decl.kind == ci.CursorKind.FIELD_DECL and owner_usr(decl) == class_usr:
                fields.add(decl.spelling)
                writes = writes or not only_read(parents)
            elif decl.kind in METHOD_KINDS and is_method(decl):
                calls.add(template_of(decl).canonical.get_usr())
    return fields, calls, writes


@functools.lru_cache(maxsize=None)
def in_scope(path: str, root: str, excludes: tuple[str, ...]) -> bool:
    path = os.path.realpath(path)
    return path.startswith(root) and not any(fnmatch.fnmatch(path, pattern) for pattern in excludes)


def methods_in(cursor: ci.Cursor, root: str, excludes: tuple[str, ...]):
    for child in cursor.get_children():
        if child.location.file and not in_scope(child.location.file.name, root, excludes):
            continue
        if is_counted_method(child):
            yield child
        elif child.kind in SCOPE_KINDS:
            yield from methods_in(child, root, excludes)


def analyse(entry: dict, root: str, excludes: tuple[str, ...], libclang: str, includes: list[str]) -> dict:
    if not ci.Config.loaded:
        ci.Config.set_library_file(libclang)
        ci.Config.set_compatibility_check(False)
    tu = ci.Index.create().parse(entry["file"], args=arguments_of(entry) + includes)
    errors = [str(d) for d in tu.diagnostics if d.severity >= ci.Diagnostic.Error]
    classes: dict[str, dict] = {}
    for cursor in methods_in(tu.cursor, root, excludes):
        class_usr = owner_usr(cursor)
        if class_usr is None:
            continue
        definition = cursor.semantic_parent.get_definition() or cursor.semantic_parent
        cls = classes.setdefault(class_usr, {
            "name": qualified_name(definition),
            "location": f"{os.path.realpath(definition.location.file.name)}:{definition.location.line}",
            "fields": sorted(f.spelling for f in definition.get_children() if f.kind == ci.CursorKind.FIELD_DECL),
            "bases": bases_of(definition), "methods": {}})
        fields, calls, writes = uses_of(cursor, class_usr, members_by_name(definition))
        cls["methods"][template_of(cursor).canonical.get_usr()] = {
            "name": cursor.spelling, "fields": sorted(fields), "calls": sorted(calls),
            "overrides": overridden_by(cursor), "reads_only": cursor.is_const_method() or not writes}
    return {"file": entry["file"], "errors": errors, "classes": classes}


def qualified_name(cursor: ci.Cursor) -> str:
    parts = []
    while cursor is not None and cursor.kind != ci.CursorKind.TRANSLATION_UNIT:
        if cursor.spelling:
            parts.append(cursor.spelling)
        cursor = cursor.semantic_parent
    return "::".join(reversed(parts))


def merge(results: list[dict]) -> dict[str, dict]:
    classes: dict[str, dict] = {}
    for result in results:
        for usr, cls in result["classes"].items():
            classes.setdefault(usr, {**cls, "methods": {}})["methods"].update(cls["methods"])
    return classes


def base_chain(class_usr: str, classes: dict[str, dict]) -> set[str]:
    chain, todo = set(), list(classes[class_usr]["bases"])
    while todo:
        base = todo.pop()
        if base not in chain and base in classes:
            chain.add(base)
            todo += classes[base]["bases"]
    return chain


def dispatched(class_usr: str, classes: dict[str, dict]) -> dict[str, set[str]]:
    """For each method, the class's own overrides that its calls into base classes reach."""
    bases = base_chain(class_usr, classes)
    owner = {usr: base for base in bases for usr in classes[base]["methods"]}
    methods = classes[class_usr]["methods"]
    override_of = {base_usr: usr for usr, method in methods.items() for base_usr in method["overrides"]}
    reached = {}
    for usr, method in methods.items():
        seen, todo = set(), [c for c in method["calls"] if c in owner or c in override_of]
        while todo:
            callee = todo.pop()
            if callee not in seen:
                seen.add(callee)
                if callee in owner:
                    todo += classes[owner[callee]]["methods"][callee]["calls"]
        reached[usr] = {override_of[c] for c in seen if c in override_of}
    return reached


def components(methods: dict[str, dict], reached: dict[str, set[str]]) -> list[list[str]]:
    parent = {usr: usr for usr in methods}

    def find(usr: str) -> str:
        while parent[usr] != usr:
            parent[usr] = parent[parent[usr]]
            usr = parent[usr]
        return usr

    by_field = collections.defaultdict(list)
    for usr, method in methods.items():
        for field in method["fields"]:
            by_field[field].append(usr)
        for callee in [*method["calls"], *reached[usr]]:
            if callee in parent:
                parent[find(usr)] = find(callee)
    for users in by_field.values():
        for usr in users[1:]:
            parent[find(usr)] = find(users[0])
    groups = collections.defaultdict(list)
    for usr in methods:
        groups[find(usr)].append(usr)
    return sorted(groups.values(), key=len, reverse=True)


def is_bare(method: dict) -> bool:
    """Alone in its part and touching nothing, or only reading what no other method touches: constructor state."""
    return not method["fields"] or method["reads_only"]


def score(classes: dict[str, dict]) -> list[dict]:
    rows = []
    for class_usr, cls in classes.items():
        methods = cls["methods"]
        if len(methods) < 2:
            continue
        parts = components(methods, dispatched(class_usr, classes))
        bare = sum(1 for part in parts if len(part) == 1 and is_bare(methods[part[0]]))
        rows.append({
            "class": cls["name"], "location": cls["location"], "lcom4": len(parts), "bare": bare,
            "jobs": max(1, len(parts) - bare), "methods": len(methods), "fields": len(cls["fields"]),
            "components": [sorted(methods[usr]["name"] for usr in part) for part in parts]})
    return sorted(rows, key=lambda r: (-r["jobs"], -r["lcom4"], -r["methods"], r["class"]))


def report(rows: list[dict], root: str, threshold: int) -> str:
    lines = [f"LCOM4 report  (threshold {threshold} job per class)", "",
             f"{'JOBS':>5} {'LCOM4':>6} {'BARE':>5} {'METHODS':>8} {'FIELDS':>7}  CLASS"]
    for row in rows:
        if row["jobs"] <= threshold:
            continue
        lines.append(f"{row['jobs']:>5} {row['lcom4']:>6} {row['bare']:>5} {row['methods']:>8} {row['fields']:>7}"
                     f"  {row['class']}")
        lines.append(f"{'':>10}{os.path.relpath(row['location'], os.path.dirname(root.rstrip(os.sep)))}")
        for part in row["components"]:
            lines.append(f"{'':>10}{{{', '.join(part)}}}")
    histogram = collections.Counter(row["jobs"] for row in rows)
    failing = sum(1 for row in rows if row["jobs"] > threshold)
    lines += ["", f"  {len(rows)} classes with 2+ methods, {failing} over the threshold; by jobs: "
              + ", ".join(f"{k}: {histogram[k]}" for k in sorted(histogram))]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(prog="cpplcom", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compile-commands", required=True, type=pathlib.Path)
    parser.add_argument("--root", required=True, type=pathlib.Path)
    parser.add_argument("--exclude", action="append", default=[], help="glob on absolute paths, repeatable")
    parser.add_argument("--json", type=pathlib.Path)
    parser.add_argument("--threshold", type=int, default=1, help="jobs (LCOM4 minus BARE) a class may have")
    parser.add_argument("--jobs", type=int, default=os.cpu_count())
    args = parser.parse_args()

    root = os.path.realpath(args.root) + os.sep
    excludes = (*DEFAULT_EXCLUDES, *args.exclude)
    entries = [{**e, "file": os.path.normpath(os.path.join(e["directory"], e["file"]))}
               for e in json.loads(args.compile_commands.read_text())]
    entries = [e for e in entries if in_scope(e["file"], root, excludes)]
    if not entries:
        sys.exit(f"no translation units under {root} in {args.compile_commands}")
    compiler = entries[0]["arguments"][0] if "arguments" in entries[0] else shlex.split(entries[0]["command"])[0]
    libclang = str(libclang_for(compiler))
    includes = system_includes(compiler, arguments_of(entries[0]))

    with concurrent.futures.ProcessPoolExecutor(args.jobs) as pool:
        results = list(pool.map(functools.partial(analyse, root=root, excludes=excludes, libclang=libclang,
                                                   includes=includes), entries))

    errors = [(r["file"], e) for r in results for e in r["errors"]]
    for file, error in errors:
        print(f"parse error in {file}: {error}", file=sys.stderr)
    rows = score(merge(results))
    print(report(rows, root, args.threshold))
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1))
    return 1 if errors or any(row["jobs"] > args.threshold for row in rows) else 0
