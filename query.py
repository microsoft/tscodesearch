"""
Structural C# AST query tool powered by tree-sitter.

Use instead of grep when you need semantically precise searches that understand
C# syntax: distinguishes type references from method calls, skips comments and
string literals, understands inheritance hierarchies.

Usage:
    query.py MODE [OPTIONS] FILE [FILE ...] [GLOB_PATTERN ...]

Modes (pick exactly one):
    --classes              List all type declarations with their base types
    --methods              List all method/constructor/property/field signatures
    --fields               List all field and property declarations with types
    --calls    METHOD      Find every call site of METHOD (ignores comments/strings).
                           METHOD may be a bare name ("Create") or a dot-qualified name
                           ("Factory.Create") to restrict to calls on a specific receiver.
    --implements TYPE      Find type declarations that inherit or implement TYPE
    --uses     TYPE        Find every place TYPE is referenced as a type
    --field-type TYPE      Find fields/properties declared with the given type
    --param-type TYPE      Find method/constructor parameters typed as TYPE
    --casts    TYPE        Find every explicit cast expression (TYPE)expr
    --ident            NAME   Find every identifier occurrence (semantic grep — skips comments/strings)
    --member-accesses  TYPE   Find all .Member accesses on locals/params declared as TYPE
    --attrs           [NAME]  List [Attribute] decorators, optionally filter by NAME
    --usings                  List all using/using-alias directives
    --declarations     NAME   Print declaration(s) named NAME (signature only; --include-body for full source)
    --params           METHOD Show the full parameter list of METHOD

Options:
    --no-path              Don't prefix output with file path (auto for single file)
    --count                Print only match counts per file + total

Examples:
    query.py --methods ItemProcessor.cs
    query.py --calls DeleteItems "$SRC_ROOT/myapp/**/*.cs"
    query.py --calls Repository.GetById "$SRC_ROOT/myapp/**/*.cs"
    query.py --member-accesses ResultType "$SRC_ROOT/myapp/services/ItemProcessor.cs"
    query.py --implements IStorageProvider "$SRC_ROOT/myapp/**/*.cs"
    query.py --uses StorageProvider "$SRC_ROOT/myapp/services/**/*.cs"
    query.py --field-type StorageProvider --search "StorageProvider"
    query.py --field-type IStorageProvider --search "IStorageProvider"
    query.py --param-type StorageProvider --search "StorageProvider"
    query.py --declarations Process ItemProcessor.cs
    query.py --classes --no-path IStorageProvider.cs
    query.py --attrs TestMethod "$SRC_ROOT/myapp/tests/**/*.cs"
    query.py --params DeleteItems StorageApi.cs
"""

import os
import re
import sys
import glob as _glob
import argparse
import json as _json
import urllib.request
import urllib.parse

_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _root)

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser
from cs_ast import (
    _TYPE_DECL_NODES, _MEMBER_DECL_NODES, _QUALIFIED_RE,
    _find_all, _text, _unqualify, _unqualify_type,
    _base_type_names, _collect_ctor_names,
    SYMBOL_KIND_TO_NODES,
)

try:
    import tree_sitter_python as tspython
    _PY_AVAILABLE = True
except ImportError:
    _PY_AVAILABLE = False


def _ts_search(collection: str, params: dict) -> dict:
    """Send a search request to Typesense over HTTP (no typesense package needed)."""
    from config import HOST, PORT, API_KEY
    qs = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    url = f"http://{HOST}:{PORT}/collections/{collection}/documents/search?{qs}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        return _json.loads(r.read())

CS = Language(tscsharp.language())
_parser = Parser(CS)

if _PY_AVAILABLE:
    _PY = Language(tspython.language())
    _py_parser = Parser(_PY)
else:
    _py_parser = None

# ── AST helpers ───────────────────────────────────────────────────────────────

def _line(node) -> int:
    """1-based line number."""
    return node.start_point[0] + 1


def _strip_generic(name: str) -> str:
    """'IFoo<T, U>' → 'IFoo'"""
    idx = name.find("<")
    return name[:idx].strip() if idx >= 0 else name.strip()


def _type_names(type_txt: str) -> set:
    """All unqualified type names that appear in a (possibly generic) type string.

    'IList<Acme.IFoo>'  → {'IList', 'IFoo'}
    'Dictionary<string, IFoo>'        → {'Dictionary', 'string', 'IFoo'}
    'IBlobStore'                      → {'IBlobStore'}
    """
    return set(re.findall(r'[A-Za-z_]\w*', _unqualify_type(type_txt)))


_LITERAL_NODES = {
    "comment", "string_literal", "verbatim_string_literal",
    "interpolated_string_expression", "character_literal",
    "interpolated_verbatim_string_expression",
}


def _in_literal(node) -> bool:
    p = node.parent
    while p:
        if p.type in _LITERAL_NODES:
            return True
        p = p.parent
    return False


def _field_type(node, src) -> str:
    """Get the type text of a field_declaration (type lives on variable_declaration child)."""
    for child in node.children:
        if child.type == "variable_declaration":
            t = child.child_by_field_name("type")
            if t:
                return _text(t, src).strip()
    return ""


# _base_type_names imported from cs_ast


def _build_sig(node, src) -> str:
    """Build 'RetType Name(Type param, ...)' for a method/ctor node."""
    ret   = node.child_by_field_name("returns") or node.child_by_field_name("type")
    name  = node.child_by_field_name("name")
    params = node.child_by_field_name("parameters")

    if not name:
        return ""

    ret_txt  = _text(ret, src).strip() if ret else ""
    name_txt = _text(name, src).strip()

    if params:
        parts = []
        for p in _find_all(params, lambda n: n.type == "parameter"):
            pt = p.child_by_field_name("type")
            pn = p.child_by_field_name("name")
            pt_txt = _text(pt, src).strip() if pt else ""
            pn_txt = _text(pn, src).strip() if pn else ""
            parts.append(f"{pt_txt} {pn_txt}".strip())
        params_txt = ", ".join(parts)
    else:
        params_txt = ""

    return f"{ret_txt} {name_txt}({params_txt})".strip() if ret_txt else f"{name_txt}({params_txt})"


# ── Query functions ───────────────────────────────────────────────────────────

def q_classes(src, tree, lines):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in _TYPE_DECL_NODES):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        kind  = node.type.replace("_declaration", "").replace("_", " ")
        name  = _text(name_node, src)
        bases = _base_type_names(node, src)
        suffix = f" : {', '.join(bases)}" if bases else ""
        results.append((_line(node), f"[{kind}] {name}{suffix}"))
    return results


def q_methods(src, tree, lines):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in _MEMBER_DECL_NODES):
        ln = _line(node)
        if node.type == "field_declaration":
            type_txt = _field_type(node, src)
            for var in _find_all(node, lambda n: n.type == "variable_declarator"):
                vn = var.child_by_field_name("name")
                if vn:
                    results.append((ln, f"[field]  {type_txt} {_text(vn, src)}"))
        elif node.type == "property_declaration":
            type_node = node.child_by_field_name("type")
            name_node = node.child_by_field_name("name")
            if name_node:
                type_txt = _text(type_node, src).strip() if type_node else ""
                results.append((ln, f"[prop]   {type_txt} {_text(name_node, src)}"))
        elif node.type == "event_declaration":
            type_node = node.child_by_field_name("type")
            name_node = node.child_by_field_name("name")
            if name_node:
                type_txt = _text(type_node, src).strip() if type_node else ""
                results.append((ln, f"[event]  {type_txt} {_text(name_node, src)}"))
        elif node.type == "event_field_declaration":
            type_txt = _field_type(node, src)
            for var in _find_all(node, lambda n: n.type == "variable_declarator"):
                vn = var.child_by_field_name("name")
                if vn:
                    results.append((ln, f"[event]  {type_txt} {_text(vn, src)}"))
        elif node.type in ("method_declaration", "local_function_statement"):
            sig = _build_sig(node, src)
            if sig:
                results.append((ln, f"[method] {sig}"))
        elif node.type == "constructor_declaration":
            sig = _build_sig(node, src)
            if sig:
                results.append((ln, f"[ctor]   {sig}"))
    return results


def q_fields(src, tree, lines):
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("field_declaration", "property_declaration")):
        ln = _line(node)
        if node.type == "field_declaration":
            type_txt = _field_type(node, src)
            for var in _find_all(node, lambda n: n.type == "variable_declarator"):
                vn = var.child_by_field_name("name")
                if vn:
                    results.append((ln, f"[field] {type_txt} {_text(vn, src)}"))
        else:
            type_node = node.child_by_field_name("type")
            type_txt  = _text(type_node, src).strip() if type_node else ""
            name_node = node.child_by_field_name("name")
            if name_node:
                results.append((ln, f"[prop]  {type_txt} {_text(name_node, src)}"))
    return results


def q_calls(src, tree, lines, method_name):
    """
    Find every call site of METHOD.

    method_name may be a bare name ("Create") or a dot-qualified name
    ("Factory.Create") to restrict matches to calls through a specific
    class or namespace prefix.
    """
    # Split "ClassName.MethodName" into qualifier + bare name
    if "." in method_name:
        qualifier, bare_name = method_name.rsplit(".", 1)
    else:
        qualifier, bare_name = None, method_name

    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type == "invocation_expression"):
        if _in_literal(node):
            continue
        fn = node.child_by_field_name("function")
        if not fn:
            continue
        matched = None
        if fn.type == "member_access_expression":
            nn   = fn.child_by_field_name("name")
            expr = fn.child_by_field_name("expression")
            if nn:
                matched = _strip_generic(_text(nn, src))
                if qualifier and matched == bare_name:
                    # Verify the expression ends with the qualifier
                    expr_txt = _text(expr, src).strip() if expr else ""
                    # strip off any leading chain: "a.B.ClassName" → check suffix
                    if not (expr_txt == qualifier
                            or expr_txt.endswith("." + qualifier)):
                        matched = None
        elif fn.type in ("identifier", "generic_name"):
            if qualifier is None:  # unqualified calls only match bare searches
                nn = fn.child_by_field_name("name") if fn.type == "generic_name" else fn
                if nn:
                    matched = _strip_generic(_text(nn, src))

        if matched == bare_name:
            raw = _text(node, src).replace("\n", " ").replace("\r", "")
            if len(raw) > 140:
                raw = raw[:140] + "…"
            results.append((_line(node), raw))

    # Constructor calls: new Foo(...) — only match when no qualifier specified
    if qualifier is None:
        for node in _find_all(tree.root_node,
                              lambda n: n.type == "object_creation_expression"):
            if _in_literal(node):
                continue
            type_node = node.child_by_field_name("type")
            if not type_node:
                continue
            idents = _find_all(type_node, lambda n: n.type == "identifier")
            if not idents:
                continue
            if _strip_generic(_text(idents[-1], src)) == bare_name:
                raw = _text(node, src).replace("\n", " ").replace("\r", "")
                if len(raw) > 140:
                    raw = raw[:140] + "…"
                results.append((_line(node), raw))

    return results


def q_accesses_of(src, tree, lines, member_name):
    """
    Find every access site of a property or field named MEMBER_NAME.

    member_name may be a bare name ("SiteKeyStore") or a dot-qualified name
    ("BlobStore.SiteKeyStore") to restrict matches to accesses through a
    specific class or namespace prefix.
    """
    if "." in member_name:
        qualifier, bare_name = member_name.rsplit(".", 1)
    else:
        qualifier, bare_name = None, member_name

    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type == "member_access_expression"):
        if _in_literal(node):
            continue
        member = node.child_by_field_name("name")
        expr   = node.child_by_field_name("expression")
        if not member:
            continue
        if _strip_generic(_text(member, src)) != bare_name:
            continue
        if qualifier:
            expr_txt = _text(expr, src).strip() if expr else ""
            if not (expr_txt == qualifier or expr_txt.endswith("." + qualifier)):
                continue
        raw = _text(node, src).replace("\n", " ").replace("\r", "")
        if len(raw) > 140:
            raw = raw[:140] + "…"
        results.append((_line(node), raw))

    return results


def q_implements(src, tree, lines, type_name):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in _TYPE_DECL_NODES):
        bases = _base_type_names(node, src)
        if not any(_strip_generic(_unqualify(b)) == type_name for b in bases):
            continue
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        kind     = node.type.replace("_declaration", "").replace("_", " ")
        name     = _text(name_node, src)
        base_str = ", ".join(bases)
        results.append((_line(node), f"[{kind}] {name} : {base_str}"))
    return results


def _q_uses_all(src, tree, lines, type_name):
    """
    Find every line where type_name is referenced as a type.
    Skips: comments, string literals, declaration names, and bare method-call identifiers.
    """
    results  = []
    seen_rows = set()

    def _is_decl_name(node):
        """Is this identifier the declared name of a class/method/field/etc.?"""
        p = node.parent
        if not p:
            return False
        nn = p.child_by_field_name("name")
        return nn is not None and nn.start_byte == node.start_byte

    def _is_invocation_target(node):
        """Is this identifier the direct callee in an invocation (not a type)?"""
        p = node.parent
        if not p:
            return False
        # Simple call: foo()
        if p.type == "invocation_expression":
            fn = p.child_by_field_name("function")
            if fn and fn.type == "identifier" and fn.start_byte == node.start_byte:
                return True
        # Member call: x.Foo() — the 'Foo' identifier
        if p.type == "member_access_expression":
            nn = p.child_by_field_name("name")
            if nn and nn.start_byte == node.start_byte:
                return True
        return False

    for node in _find_all(tree.root_node, lambda n: n.type == "identifier"):
        if _text(node, src) != type_name:
            continue
        if _in_literal(node):
            continue
        if _is_decl_name(node):
            continue
        if _is_invocation_target(node):
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        line_text = lines[row].strip() if row < len(lines) else ""
        results.append((_line(node), line_text))
    return results


def q_attrs(src, tree, lines, attr_name=None):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "attribute"):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        aname       = _text(name_node, src).strip()
        aname_short = aname[:-len("Attribute")] if aname.endswith("Attribute") else aname
        aname_unqual = _unqualify(aname_short)
        if attr_name:
            if aname_unqual != attr_name and aname_short != attr_name and aname != attr_name:
                continue
        args_node = node.child_by_field_name("arguments")
        args_txt  = _text(args_node, src).strip() if args_node else ""
        results.append((_line(node), f"[{aname}]{args_txt}"))
    return results


def q_usings(src, tree, lines):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "using_directive"):
        full = _text(node, src).strip().rstrip(";")
        results.append((_line(node), full))
    return results


def q_declarations(src, tree, lines, name, include_body=False, symbol_kind=None):
    """Find every method/type/property declaration named NAME.

    By default returns only the signature (lines up to but not including the
    body block), so call sites inside the body do not bleed into the output at
    wrong line locations.  Pass include_body=True to get the full source span.
    For members without a body (interface methods, abstract declarations) the
    full single-line text is returned regardless.

    symbol_kind: optional filter restricting which declaration kinds are
    returned.  Accepted values: method, constructor, property, field, event,
    class, interface, struct, enum, record, delegate, type, member.
    """
    kind_nodes = SYMBOL_KIND_TO_NODES.get((symbol_kind or "").lower().strip())
    target_nodes = kind_nodes if kind_nodes is not None else (_TYPE_DECL_NODES | _MEMBER_DECL_NODES)
    results = []
    all_targets = _find_all(
        tree.root_node,
        lambda n: n.type in target_nodes
    )
    for node in all_targets:
        name_node = node.child_by_field_name("name")
        if not name_node or _text(name_node, src).strip() != name:
            continue
        kind      = node.type.replace("_declaration", "").replace("statement", "").replace("_", " ").strip()
        start_row = node.start_point[0]
        end_row   = node.end_point[0]

        body_node = node.child_by_field_name("body")
        if body_node and not include_body:
            # Signature only — stop before the opening {
            sig_end_row = body_node.start_point[0]
            content = "\n".join(lines[start_row:sig_end_row]).rstrip()
        else:
            content = "\n".join(lines[start_row:end_row + 1])

        header = f"── [{kind}] {name}  (lines {start_row + 1}–{end_row + 1}) ──"
        results.append((_line(node), f"{header}\n{content}"))
    return results


def q_params(src, tree, lines, method_name):
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("method_declaration",
                                               "constructor_declaration",
                                               "local_function_statement")):
        name_node = node.child_by_field_name("name")
        if not name_node or _text(name_node, src).strip() != method_name:
            continue
        params_node = node.child_by_field_name("parameters")
        if not params_node:
            results.append((_line(node), "(no parameters)"))
            continue
        param_lines = []
        for p in _find_all(params_node, lambda n: n.type == "parameter"):
            pt   = p.child_by_field_name("type")
            pn   = p.child_by_field_name("name")
            pt_t = _text(pt, src).strip() if pt else ""
            pn_t = _text(pn, src).strip() if pn else ""
            # Default value: no named "default" field in this grammar —
            # the '=' and value are direct children of the parameter node.
            df_t = ""
            children = p.children
            for idx, ch in enumerate(children):
                if not ch.is_named and _text(ch, src).strip() == "=" and idx + 1 < len(children):
                    df_t = f" = {_text(children[idx + 1], src).strip()}"
                    break
            # Modifiers (ref/out/in/params) are "modifier" nodes in this grammar.
            mods = [_text(c, src) for c in children
                    if c.is_named and c.type in ("modifier", "parameter_modifier")]
            mod_t = " ".join(mods) + " " if mods else ""
            param_lines.append(f"  {mod_t}{pt_t} {pn_t}{df_t}".rstrip())
        results.append((_line(node), "\n".join(param_lines) or "(no parameters)"))
    return results


def _q_field_type(src, tree, lines, type_name):
    """
    Find fields and properties whose declared type is (or starts with) TYPE.

    Useful for migration analysis: find all 'ConcreteStore _foo' fields that
    should be changed to 'IStorageProvider _foo'.

    TYPE matching is exact on the bare (non-generic) name, so 'IFoo' matches
    both 'IFoo' and 'IFoo<T>'.
    """
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("field_declaration",
                                               "event_field_declaration",
                                               "property_declaration")):
        if node.type in ("field_declaration", "event_field_declaration"):
            type_txt = _field_type(node, src)
            if type_name not in _type_names(type_txt):
                continue
            label = "[field]" if node.type == "field_declaration" else "[event]"
            for var in _find_all(node, lambda n: n.type == "variable_declarator"):
                vn = var.child_by_field_name("name")
                if vn:
                    # Look up enclosing class name for context
                    cls = _enclosing_type_name(node, src)
                    cls_prefix = f"[in {cls}] " if cls else ""
                    results.append((_line(node),
                                    f"{label} {type_txt} {_text(vn, src)}  {cls_prefix}"))
        else:  # property_declaration
            type_node = node.child_by_field_name("type")
            if not type_node:
                continue
            type_txt = _text(type_node, src).strip()
            if type_name not in _type_names(type_txt):
                continue
            name_node = node.child_by_field_name("name")
            if name_node:
                cls = _enclosing_type_name(node, src)
                cls_prefix = f"[in {cls}] " if cls else ""
                results.append((_line(node),
                                 f"[prop]  {type_txt} {_text(name_node, src)}  {cls_prefix}"))
    return results


def _q_param_type(src, tree, lines, type_name):
    """
    Find method/constructor parameters whose type is TYPE.

    Useful for migration analysis: find all 'ConcreteStore store' parameters
    in method signatures that should be changed to 'IStorageProvider store'.

    TYPE matching is exact on the bare (non-generic) name.
    """
    results = []
    method_nodes = _find_all(
        tree.root_node,
        lambda n: n.type in ("method_declaration", "constructor_declaration",
                              "local_function_statement", "delegate_declaration",
                              "lambda_expression"),
    )
    for mnode in method_nodes:
        params_node = mnode.child_by_field_name("parameters")
        if not params_node:
            continue
        # Get the method/ctor name for context
        name_node = mnode.child_by_field_name("name")
        mname = _text(name_node, src).strip() if name_node else "<lambda>"
        kind  = mnode.type.replace("_declaration", "").replace("statement", "").replace("_", " ").strip()

        for p in _find_all(params_node, lambda n: n.type == "parameter"):
            pt = p.child_by_field_name("type")
            if not pt:
                continue
            pt_txt = _text(pt, src).strip()
            if type_name not in _type_names(pt_txt):
                continue
            pn = p.child_by_field_name("name")
            pn_txt = _text(pn, src).strip() if pn else ""
            mods = [_text(c, src) for c in p.children
                    if c.is_named and c.type == "parameter_modifier"]
            mod_t = " ".join(mods) + " " if mods else ""
            results.append((_line(p),
                             f"[{kind}] {mname}({mod_t}{pt_txt} {pn_txt})"))
    return results


def _q_return_type(src, tree, lines, type_name):
    """
    Find method/constructor declarations whose return type references TYPE.

    TYPE matching is exact on the bare (non-generic) name.
    """
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("method_declaration",
                                               "constructor_declaration",
                                               "local_function_statement")):
        # Check return type only
        type_node = node.child_by_field_name("type")
        ret_type_txt = _text(type_node, src).strip() if type_node else ""
        if type_name not in _type_names(ret_type_txt):
            continue

        name_node = node.child_by_field_name("name")
        mname = _text(name_node, src).strip() if name_node else "<anonymous>"

        # Build param list text
        params_node = node.child_by_field_name("parameters")
        param_parts = []
        if params_node:
            for p in _find_all(params_node, lambda n: n.type == "parameter"):
                pt = p.child_by_field_name("type")
                pn = p.child_by_field_name("name")
                pt_t = _text(pt, src).strip() if pt else ""
                pn_t = _text(pn, src).strip() if pn else ""
                param_parts.append(f"{pt_t} {pn_t}".strip())
        params_text = ", ".join(param_parts)

        sig_text = f"{ret_type_txt} {mname}({params_text})".strip()
        results.append((_line(node), sig_text))
    return results


def _q_local_type(src, tree, lines, type_name):
    """
    Find local variable declarations whose type is TYPE.

    Matches 'BlobStore store = ...;' statements inside method bodies.
    TYPE matching is exact on the bare (non-generic) name.
    """
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type == "local_declaration_statement"):
        var_decl = next((c for c in node.children if c.type == "variable_declaration"), None)
        if not var_decl:
            continue
        type_node = var_decl.child_by_field_name("type")
        if not type_node:
            continue
        type_txt = _text(type_node, src).strip()
        if type_name not in _type_names(type_txt):
            continue
        cls = _enclosing_type_name(node, src)
        cls_prefix = f"[in {cls}] " if cls else ""
        for var in _find_all(var_decl, lambda n: n.type == "variable_declarator"):
            vn = var.child_by_field_name("name")
            if vn:
                results.append((_line(node),
                                 f"[local] {type_txt} {_text(vn, src)}  {cls_prefix}"))
    return results


def _q_base_uses(src, tree, lines, type_name):
    """
    Find type declarations (class/interface/struct/record) that have type_name
    in their base list. Uses _type_names for flexible matching (handles generics).
    Returns the declaration header line.
    """
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in _TYPE_DECL_NODES):
        bases = _base_type_names(node, src)
        if not any(type_name in _type_names(b) for b in bases):
            continue
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        kind     = node.type.replace("_declaration", "").replace("_", " ")
        name     = _text(name_node, src)
        base_str = ", ".join(bases)
        results.append((_line(node), f"[{kind}] {name} : {base_str}"))
    return results


def q_uses(src, tree, lines, type_name, uses_kind=None):
    """Find usages of TYPE in type-annotation positions.

    uses_kind: 'all' (default), 'field', 'param', 'return', 'cast', 'base', 'locals'
    """
    k = (uses_kind or "all").lower().strip()
    if k == "field":
        return _q_field_type(src, tree, lines, type_name)
    elif k == "param":
        return _q_param_type(src, tree, lines, type_name)
    elif k == "return":
        return _q_return_type(src, tree, lines, type_name)
    elif k == "cast":
        return q_casts(src, tree, lines, type_name)
    elif k == "base":
        return _q_base_uses(src, tree, lines, type_name)
    elif k == "locals":
        return _q_local_type(src, tree, lines, type_name)
    else:
        return _q_uses_all(src, tree, lines, type_name)



def q_casts(src, tree, lines, type_name):
    """
    Find every explicit cast expression (TYPE)expr in source code.

    Useful for migration analysis: find all '(ConcreteStore)x' casts that
    should be replaced with 'ConcreteStore.From(x)'.

    TYPE matching is exact on the bare (non-generic) name.
    """
    results = []
    seen_rows = set()
    for node in _find_all(tree.root_node, lambda n: n.type == "cast_expression"):
        if _in_literal(node):
            continue
        type_node = node.child_by_field_name("type")
        if not type_node:
            continue
        cast_type = _strip_generic(_unqualify(_text(type_node, src).strip()))
        if cast_type != type_name:
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        line_text = lines[row].strip() if row < len(lines) else ""
        results.append((_line(node), line_text))
    return results


def _get_init_expr(declarator):
    """Return the RHS expression from a variable_declarator.

    In tree-sitter-c-sharp the initializer expression is the third child
    (after identifier and '=') — there is no equals_value_clause wrapper.
    """
    children = declarator.children
    if len(children) >= 3 and children[1].type == "=":
        return children[2]
    return None


def q_accesses_on(src, tree, lines, type_name):
    """
    Find all .Member accesses on local variables and parameters declared as TYPE.

    Useful for discovering which properties callers read from a value after
    receiving it — e.g. what fields callers read from a result object after a factory call.

    Handles both explicitly typed declarations and var-inferred locals:
      - var x = new TypeName(...)      object creation
      - var x = new TypeName[n]        array creation  (x treated as TypeName[])
      - var x = arr[i]                 element access on a TypeName[] array
      - var x = expr as TypeName       as-cast
      - var x = (TypeName)expr         explicit cast
    """
    var_names   = set()  # variables directly typed as TypeName
    array_names = set()  # variables typed as TypeName[] (element access → var_names)

    # ── Step 1: explicitly typed locals ─────────────────────────────────────
    for node in _find_all(tree.root_node, lambda n: n.type == "variable_declaration"):
        type_node = node.child_by_field_name("type")
        if not type_node:
            continue
        if type_name not in _type_names(_text(type_node, src).strip()):
            continue
        for decl in _find_all(node, lambda n: n.type == "variable_declarator"):
            vn = decl.child_by_field_name("name")
            if vn:
                var_names.add(_text(vn, src).strip())

    # ── Step 2: explicitly typed parameters ─────────────────────────────────
    for node in _find_all(tree.root_node, lambda n: n.type == "parameter"):
        pt = node.child_by_field_name("type")
        if not pt:
            continue
        if type_name not in _type_names(_text(pt, src).strip()):
            continue
        pn = node.child_by_field_name("name")
        if pn:
            var_names.add(_text(pn, src).strip())

    # ── Step 3a: var-inferred locals (object/array creation, casts) ─────────
    for node in _find_all(tree.root_node, lambda n: n.type == "variable_declaration"):
        type_node = node.child_by_field_name("type")
        if not type_node or _text(type_node, src).strip() != "var":
            continue
        for decl in _find_all(node, lambda n: n.type == "variable_declarator"):
            vn = decl.child_by_field_name("name")
            if not vn:
                continue
            expr = _get_init_expr(decl)
            if not expr:
                continue
            name = _text(vn, src).strip()
            if expr.type == "object_creation_expression":
                t = expr.child_by_field_name("type")
                if t and type_name in _type_names(_text(t, src)):
                    var_names.add(name)
            elif expr.type == "array_creation_expression":
                # new TypeName[n] — element type lives inside array_type child
                t = expr.child_by_field_name("type")
                if t:
                    elem = t.child_by_field_name("type") if t.type == "array_type" else t
                    if type_name in _type_names(_text(elem, src)):
                        array_names.add(name)
            elif expr.type == "cast_expression":
                t = expr.child_by_field_name("type")
                if t and type_name in _type_names(_text(t, src)):
                    var_names.add(name)
            elif expr.type == "as_expression":
                # Grammar: as_expression has "right" field for the target type.
                t = expr.child_by_field_name("right") or expr.child_by_field_name("type")
                if t and type_name in _type_names(_text(t, src)):
                    var_names.add(name)

    # ── Step 3b: var x = arr[i] where arr is a known TypeName[] ─────────────
    if array_names:
        for node in _find_all(tree.root_node, lambda n: n.type == "variable_declaration"):
            type_node = node.child_by_field_name("type")
            if not type_node or _text(type_node, src).strip() != "var":
                continue
            for decl in _find_all(node, lambda n: n.type == "variable_declarator"):
                vn = decl.child_by_field_name("name")
                if not vn:
                    continue
                expr = _get_init_expr(decl)
                if not expr or expr.type != "element_access_expression":
                    continue
                obj = expr.child_by_field_name("expression")
                if obj and obj.type == "identifier" and _text(obj, src).strip() in array_names:
                    var_names.add(_text(vn, src).strip())

    if not var_names:
        return []

    # ── Step 4: find .Member accesses on all collected variable names ────────
    results = []
    seen_rows = set()
    for node in _find_all(tree.root_node,
                          lambda n: n.type == "member_access_expression"):
        if _in_literal(node):
            continue
        obj    = node.child_by_field_name("expression")
        member = node.child_by_field_name("name")
        if not obj or not member:
            continue
        if obj.type != "identifier":
            continue
        if _text(obj, src).strip() not in var_names:
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        member_name = _text(member, src).strip()
        line_text   = lines[row].strip() if row < len(lines) else ""
        results.append((_line(node), f".{member_name}  ← {line_text}"))
    return results




def q_all_refs(src, tree, lines, name):
    """
    Find every occurrence of identifier NAME in source code.

    This is a semantic grep: it skips comments and string literals but otherwise
    matches any syntactic context — type declarations, field names, method names,
    call sites, cast targets, local variables, etc.

    Complements the focused modes (uses/calls/field_type/casts) by giving a
    complete picture of every line that references the symbol.
    """
    results = []
    seen_rows = set()
    for node in _find_all(tree.root_node, lambda n: n.type == "identifier"):
        if _text(node, src) != name:
            continue
        if _in_literal(node):
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        line_text = lines[row].strip() if row < len(lines) else ""
        results.append((_line(node), line_text))
    return results




# ── Internal helper ───────────────────────────────────────────────────────────

def _enclosing_type_name(node, src) -> str:
    """Walk up the AST to find the nearest enclosing type declaration's name."""
    p = node.parent
    while p:
        if p.type in _TYPE_DECL_NODES:
            nn = p.child_by_field_name("name")
            if nn:
                return _text(nn, src).strip()
        p = p.parent
    return ""


# ── Typesense file resolver ───────────────────────────────────────────────────

def files_from_search(query, sub=None, ext="cs", limit=50,
                       collection=None, src_root=None,
                       query_by=None):
    """
    Run a Typesense search and return the local file paths of matching documents.
    Faster than globbing when you already know roughly which files are relevant.

    collection: Typesense collection name (defaults to COLLECTION from config).
    src_root:   Source root directory for constructing absolute paths
                (defaults to SRC_ROOT from config).
    query_by:   Typesense query_by field list override.  Defaults to broad full-text
                search ("filename,class_names,method_names,tokens").
                For listing modes (methods/fields/classes) pass a signature-focused
                string like "member_sigs,class_names,base_types,type_refs,method_names,filename"
                to avoid pulling in files that only mention the term in call sites or
                comments (which would cause unrelated method/field defs to appear).
    """
    from config import COLLECTION, SRC_ROOT

    from config import to_native_path
    coll_name = collection or COLLECTION
    root = src_root or SRC_ROOT
    src_root_native = to_native_path(root)

    filter_parts = [f"extension:={ext.lstrip('.')}"] if ext else []
    if sub:
        filter_parts.append(f"subsystem:={sub}")

    params = {
        "q":         query,
        "query_by":  query_by or "filename,symbols,class_names,method_names,content",
        "per_page":  limit,
        "prefix":    "false",
        "num_typos": "1",
    }
    if filter_parts:
        params["filter_by"] = " && ".join(filter_parts)

    try:
        result = _ts_search(coll_name, params)
    except Exception as e:
        print(f"Typesense search error: {e}", file=sys.stderr)
        print("Is the server running? Try: ts start", file=sys.stderr)
        return []

    paths = []
    seen  = set()
    for hit in result.get("hits", []):
        doc = hit["document"]
        rel = doc.get("relative_path", "")
        if not rel:
            continue
        # Construct native OS path from src_root + relative_path
        path = os.path.join(src_root_native, rel.replace("/", os.sep))
        if path not in seen and os.path.isfile(path):
            seen.add(path)
            paths.append(path)

    found = result.get("found", len(paths))
    print(f"[search] '{query}' → {found} index hits, {len(paths)} local files",
          file=sys.stderr)
    return paths


# ── Python AST query functions ────────────────────────────────────────────────

_PY_LITERAL_NODES = {"comment", "string", "concatenated_string"}


def _py_in_literal(node) -> bool:
    p = node.parent
    while p:
        if p.type in _PY_LITERAL_NODES:
            return True
        p = p.parent
    return False


def _py_enclosing_class(node, src) -> str:
    """Walk up the AST to find the nearest enclosing class name."""
    p = node.parent
    while p:
        if p.type == "class_definition":
            nn = p.child_by_field_name("name")
            if nn:
                return _text(nn, src).strip()
        p = p.parent
    return ""


def _py_base_names(node, src) -> list:
    """Extract base class names from a class_definition's superclasses."""
    names = []
    superclasses = node.child_by_field_name("superclasses")
    if not superclasses:
        return names
    for child in superclasses.named_children:
        if child.type == "identifier":
            names.append(_text(child, src).strip())
        elif child.type == "attribute":
            attr = child.child_by_field_name("attribute")
            if attr:
                names.append(_text(attr, src).strip())
        elif child.type == "subscript":
            val = child.child_by_field_name("value")
            if val and val.type == "identifier":
                names.append(_text(val, src).strip())
    return names


def py_q_classes(src, tree, lines):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "class_definition"):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _text(name_node, src).strip()
        bases = _py_base_names(node, src)
        suffix = f"({', '.join(bases)})" if bases else ""
        results.append((_line(node), f"[class] {name}{suffix}"))
    return results


def py_q_methods(src, tree, lines):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "function_definition"):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        name = _text(name_node, src).strip()
        params_node = node.child_by_field_name("parameters")
        return_node = node.child_by_field_name("return_type")
        params_str = _text(params_node, src).strip() if params_node else "()"
        ret_str = f" -> {_text(return_node, src).strip()}" if return_node else ""
        cls = _py_enclosing_class(node, src)
        kind = "method" if cls else "def"
        cls_prefix = f"[in {cls}] " if cls else ""
        results.append((_line(node), f"[{kind}] {cls_prefix}{name}{params_str}{ret_str}"))
    return results


def py_q_calls(src, tree, lines, method_name):
    results = []
    seen_rows = set()
    for node in _find_all(tree.root_node, lambda n: n.type == "call"):
        if _py_in_literal(node):
            continue
        fn = node.child_by_field_name("function")
        if not fn:
            continue
        matched = None
        if fn.type == "identifier":
            matched = _text(fn, src).strip()
        elif fn.type == "attribute":
            attr = fn.child_by_field_name("attribute")
            if attr:
                matched = _text(attr, src).strip()
        if matched != method_name:
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        raw = _text(node, src).replace("\n", " ").replace("\r", "")
        if len(raw) > 140:
            raw = raw[:140] + "…"
        results.append((_line(node), raw))
    return results


def py_q_implements(src, tree, lines, base_name):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "class_definition"):
        name_node = node.child_by_field_name("name")
        if not name_node:
            continue
        bases = _py_base_names(node, src)
        if base_name not in bases:
            continue
        name = _text(name_node, src).strip()
        results.append((_line(node), f"[class] {name}({', '.join(bases)})"))
    return results


def py_q_ident(src, tree, lines, name):
    results = []
    seen_rows = set()
    for node in _find_all(tree.root_node, lambda n: n.type == "identifier"):
        if _text(node, src) != name:
            continue
        if _py_in_literal(node):
            continue
        row = node.start_point[0]
        if row in seen_rows:
            continue
        seen_rows.add(row)
        line_text = lines[row].strip() if row < len(lines) else ""
        results.append((_line(node), line_text))
    return results


def py_q_declarations(src, tree, lines, name, include_body=False, symbol_kind=None):
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("function_definition", "class_definition")):
        name_node = node.child_by_field_name("name")
        if not name_node or _text(name_node, src).strip() != name:
            continue
        kind = "class" if node.type == "class_definition" else "def"
        start_row = node.start_point[0]
        end_row = node.end_point[0]
        body_lines = "\n".join(lines[start_row:end_row + 1])
        header = f"── [{kind}] {name}  (lines {start_row + 1}–{end_row + 1}) ──"
        results.append((_line(node), f"{header}\n{body_lines}"))
    return results


def py_q_decorators(src, tree, lines, name=None):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "decorator"):
        full = _text(node, src).strip()
        dname = full.lstrip("@").split("(")[0].split(".")[-1].strip()
        if name and dname != name:
            continue
        results.append((_line(node), full))
    return results


def py_q_imports(src, tree, lines):
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("import_statement", "import_from_statement")):
        results.append((_line(node), _text(node, src).strip()))
    return results


def py_q_params(src, tree, lines, method_name):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "function_definition"):
        name_node = node.child_by_field_name("name")
        if not name_node or _text(name_node, src).strip() != method_name:
            continue
        params_node = node.child_by_field_name("parameters")
        if not params_node:
            results.append((_line(node), "(no parameters)"))
            continue
        param_lines = []
        for p in params_node.named_children:
            if p.type == "identifier":
                param_lines.append(f"  {_text(p, src)}")
            elif p.type in ("typed_parameter", "typed_default_parameter"):
                # Name is the first identifier child (not a named field in tree-sitter-python)
                pname = next((_text(c, src) for c in p.named_children
                              if c.type == "identifier"), "")
                ptype = p.child_by_field_name("type")
                pt_txt = f": {_text(ptype, src)}" if ptype else ""
                dval = p.child_by_field_name("value") if p.type == "typed_default_parameter" else None
                dv_txt = f" = {_text(dval, src)}" if dval else ""
                param_lines.append(f"  {pname}{pt_txt}{dv_txt}")
            elif p.type == "default_parameter":
                pname = next((_text(c, src) for c in p.named_children
                              if c.type == "identifier"), "")
                dval = p.child_by_field_name("value")
                dv_txt = f" = {_text(dval, src)}" if dval else ""
                param_lines.append(f"  {pname}{dv_txt}")
            elif p.type in ("list_splat_pattern", "dictionary_splat_pattern",
                            "keyword_separator", "positional_separator"):
                param_lines.append(f"  {_text(p, src)}")
        results.append((_line(node), "\n".join(param_lines) or "(no parameters)"))
    return results


# ── Python file processing ────────────────────────────────────────────────────

def process_py_file(path, mode, mode_arg, show_path, count_only, context=0, src_root=None, include_body=False, symbol_kind=None, uses_kind=None):
    if not _PY_AVAILABLE or _py_parser is None:
        print(f"ERROR: tree-sitter-python not installed. "
              f"Run: pip install tree-sitter-python", file=sys.stderr)
        return 0
    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return 0
    try:
        tree = _py_parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return 0

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":    lambda: py_q_classes(src_bytes, tree, lines),
        "methods":    lambda: py_q_methods(src_bytes, tree, lines),
        "calls":      lambda: py_q_calls(src_bytes, tree, lines, mode_arg),
        "implements": lambda: py_q_implements(src_bytes, tree, lines, mode_arg),
        "ident":         lambda: py_q_ident(src_bytes, tree, lines, mode_arg),
        "declarations":  lambda: py_q_declarations(src_bytes, tree, lines, mode_arg),
        "decorators":    lambda: py_q_decorators(src_bytes, tree, lines, mode_arg),
        "imports":    lambda: py_q_imports(src_bytes, tree, lines),
        "params":     lambda: py_q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if not fn:
        print(f"Unknown mode: {mode!r}", file=sys.stderr)
        return 0

    results = fn()
    if not results:
        return 0

    from config import SRC_ROOT as _SRC_ROOT
    _effective_root = (src_root or _SRC_ROOT).rstrip("/").replace("\\", "/")
    _path_norm = path.replace("\\", "/")
    if _effective_root and _path_norm.lower().startswith(_effective_root.lower() + "/"):
        _disp_base = _path_norm[len(_effective_root) + 1:]
    else:
        _disp_base = _path_norm

    if count_only:
        print(f"{len(results):4d}  {_disp_base}")
        return len(results)

    disp_path = _disp_base
    for line_num_str, text in results:
        if show_path:
            print(f"{disp_path}:{line_num_str}: {text}")
        else:
            print(f"{line_num_str}: {text}")

        if context > 0 and mode != "declarations":
            try:
                row = int(line_num_str) - 1
                start = max(0, row - context)
                end = min(len(lines), row + context + 1)
                for i, ln in enumerate(lines[start:end], start):
                    if i == row:
                        continue
                    prefix = f"  {disp_path}:{i + 1}-" if show_path else f"  {i + 1}-"
                    print(f"{prefix} {ln}")
                print()
            except (ValueError, IndexError):
                pass
    return len(results)


# ── C# file processing ────────────────────────────────────────────────────────

def process_file(path, mode, mode_arg, show_path, count_only, context=0, src_root=None, include_body=False, symbol_kind=None, uses_kind=None):
    try:
        src_bytes = open(path, "rb").read()
    except OSError as e:
        print(f"ERROR reading {path}: {e}", file=sys.stderr)
        return 0

    try:
        tree = _parser.parse(src_bytes)
    except Exception as e:
        print(f"ERROR parsing {path}: {e}", file=sys.stderr)
        return 0

    lines = src_bytes.decode("utf-8", errors="replace").splitlines()

    dispatch = {
        "classes":         lambda: q_classes(src_bytes, tree, lines),
        "methods":         lambda: q_methods(src_bytes, tree, lines),
        "fields":          lambda: q_fields(src_bytes, tree, lines),
        "calls":           lambda: q_calls(src_bytes, tree, lines, mode_arg),
        "implements":      lambda: q_implements(src_bytes, tree, lines, mode_arg),
        "uses":            lambda: q_uses(src_bytes, tree, lines, mode_arg, uses_kind=uses_kind),
        "accesses_on":     lambda: q_accesses_on(src_bytes, tree, lines, mode_arg),
        "all_refs":        lambda: q_all_refs(src_bytes, tree, lines, mode_arg),
        "casts":           lambda: q_casts(src_bytes, tree, lines, mode_arg),
        "attrs":           lambda: q_attrs(src_bytes, tree, lines, mode_arg),
        "accesses_of":     lambda: q_accesses_of(src_bytes, tree, lines, mode_arg),
        "usings":          lambda: q_usings(src_bytes, tree, lines),
        "declarations":    lambda: q_declarations(src_bytes, tree, lines, mode_arg, include_body=include_body, symbol_kind=symbol_kind),
        "params":          lambda: q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if not fn:
        return 0

    results = fn()
    if not results:
        return 0

    # Strip src_root prefix so paths are shown relative to the search root
    from config import SRC_ROOT as _SRC_ROOT
    _effective_root = (src_root or _SRC_ROOT).rstrip("/").replace("\\", "/")
    _path_norm = path.replace("\\", "/")
    if _effective_root and _path_norm.lower().startswith(_effective_root.lower() + "/"):
        _disp_base = _path_norm[len(_effective_root) + 1:]
    else:
        _disp_base = _path_norm

    if count_only:
        print(f"{len(results):4d}  {_disp_base}")
        return len(results)

    disp_path = _disp_base
    for line_num_str, text in results:
        if show_path:
            print(f"{disp_path}:{line_num_str}: {text}")
        else:
            print(f"{line_num_str}: {text}")

        # Optional surrounding context lines (like grep -C)
        if context > 0 and mode != "declarations":
            try:
                row = int(line_num_str) - 1  # convert back to 0-based
                start = max(0, row - context)
                end   = min(len(lines), row + context + 1)
                for i, ln in enumerate(lines[start:end], start):
                    if i == row:
                        continue  # already printed as the match line
                    prefix = f"  {disp_path}:{i + 1}-" if show_path else f"  {i + 1}-"
                    print(f"{prefix} {ln}")
                print()
            except (ValueError, IndexError):
                pass
    return len(results)


# ── Glob expansion ────────────────────────────────────────────────────────────

def expand_files(patterns):
    files = []
    seen  = set()
    for pat in patterns:
        pat = pat.replace("\\", "/")
        if any(c in pat for c in ("*", "?")):
            for f in sorted(_glob.glob(pat, recursive=True)):
                f = f.replace("\\", "/")
                if f.endswith(".cs") and f not in seen:
                    seen.add(f)
                    files.append(f)
        elif os.path.isdir(pat):
            for root, _, fnames in os.walk(pat):
                for fn in sorted(fnames):
                    if fn.endswith(".cs"):
                        fp = os.path.join(root, fn).replace("\\", "/")
                        if fp not in seen:
                            seen.add(fp)
                            files.append(fp)
        elif os.path.isfile(pat) and pat not in seen:
            seen.add(pat)
            files.append(pat)
    return files


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mg = ap.add_mutually_exclusive_group(required=True)
    mg.add_argument("--classes",    action="store_true",
                    help="List all type declarations")
    mg.add_argument("--methods",    action="store_true",
                    help="List all method/field/property signatures")
    mg.add_argument("--fields",     action="store_true",
                    help="List all field and property declarations")
    mg.add_argument("--calls",      metavar="METHOD",
                    help="Find every call site of METHOD")
    mg.add_argument("--implements", metavar="TYPE",
                    help="Find types inheriting/implementing TYPE")
    mg.add_argument("--uses",       metavar="TYPE",
                    help="Find all type references to TYPE (not comments/strings)")
    mg.add_argument("--casts",      metavar="TYPE",
                    help="Find every explicit cast expression (TYPE)expr")
    mg.add_argument("--all-refs",         metavar="NAME",
                    help="Find every identifier occurrence (semantic grep, skips comments/strings)")
    mg.add_argument("--accesses-of",      metavar="MEMBER",
                    help="Find every access site of property/field MEMBER (optionally Class.MEMBER)")
    mg.add_argument("--attrs",            metavar="NAME", nargs="?", const="",
                    help="List [Attribute] decorators (optionally filter by NAME)")
    mg.add_argument("--usings",     action="store_true",
                    help="List all using directives")
    mg.add_argument("--declarations", metavar="NAME",
                    help="Print declaration(s) named NAME (signature by default; --include-body for full source)")
    mg.add_argument("--params",     metavar="METHOD",
                    help="Show parameter list of METHOD")

    ap.add_argument("files", nargs="*", metavar="FILE_OR_PATTERN",
                    help="Files, directories, or glob patterns (** for recursive). "
                         "Omit when using --search.")
    ap.add_argument("--search",       metavar="QUERY",
                    help="Use Typesense to find files matching QUERY instead of globs. "
                         "Much faster than globbing for targeted searches.")
    ap.add_argument("--search-sub",   metavar="SUBSYSTEM",
                    help="Filter Typesense search by subsystem (e.g. myapp, services)")
    ap.add_argument("--search-ext",   metavar="EXT", default="cs",
                    help="Filter Typesense search by extension (default: cs)")
    ap.add_argument("--search-limit", metavar="N", type=int, default=50,
                    help="Max files to fetch from Typesense (default: 50)")
    ap.add_argument("--uses-kind", metavar="KIND", default="",
                    help="For --uses: narrow to field, param, return, cast, or base")
    ap.add_argument("--no-path", action="store_true",
                    help="Omit file path prefix (auto-set for single files)")
    ap.add_argument("--count",   action="store_true",
                    help="Print only match counts per file + total")
    ap.add_argument("--context", metavar="N", type=int, default=0,
                    help="Show N surrounding source lines around each match (like grep -C)")
    args = ap.parse_args()

    if not args.files and not args.search:
        ap.error("Provide FILE_OR_PATTERN arguments or use --search QUERY")

    # Resolve mode + arg
    if args.classes:
        mode, mode_arg = "classes",    None
    elif args.methods:
        mode, mode_arg = "methods",    None
    elif args.fields:
        mode, mode_arg = "fields",     None
    elif args.calls:
        mode, mode_arg = "calls",      args.calls
    elif args.implements:
        mode, mode_arg = "implements", args.implements
    elif args.uses:
        mode, mode_arg = "uses",       args.uses
    elif args.casts:
        mode, mode_arg = "casts",      args.casts
    elif args.all_refs:
        mode, mode_arg = "all_refs",   args.all_refs
    elif args.accesses_of:
        mode, mode_arg = "accesses_of",      args.accesses_of
    elif args.attrs is not None:
        mode, mode_arg = "attrs",      args.attrs or None
    elif args.usings:
        mode, mode_arg = "usings",     None
    elif args.declarations:
        mode, mode_arg = "declarations", args.declarations
    elif args.params:
        mode, mode_arg = "params",     args.params
    else:
        ap.print_help(); sys.exit(1)

    if args.search:
        files = files_from_search(
            query=args.search,
            sub=getattr(args, "search_sub", None),
            ext=getattr(args, "search_ext", "cs"),
            limit=getattr(args, "search_limit", 50),
        )
        if not files:
            print("No matching files found in index.", file=sys.stderr)
            sys.exit(1)
    else:
        files = expand_files(args.files)
        if not files:
            print(f"No .cs files found: {' '.join(args.files)}", file=sys.stderr)
            sys.exit(1)

    has_glob  = any(c in p for p in (args.files or []) for c in ("*", "?"))
    show_path = not args.no_path and (len(files) > 1 or has_glob or bool(args.search))

    uses_kind = getattr(args, "uses_kind", "") or ""
    total = 0
    for f in files:
        total += process_file(f, mode, mode_arg, show_path, args.count, context=args.context, uses_kind=uses_kind)

    if args.count:
        print(f"\nTotal: {total}")
    elif len(files) > 1:
        print(f"\n({total} matches across {len(files)} files)", file=sys.stderr)


if __name__ == "__main__":
    main()
