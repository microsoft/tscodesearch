"""
C# AST query functions — extracted from query.py.

All public functions are re-exported from query.py for backward compatibility.
"""

import re
import sys

from ast_cs import (
    _TYPE_DECL_NODES, _MEMBER_DECL_NODES, _QUALIFIED_RE,
    _find_all, _text, _unqualify, _unqualify_type,
    _base_type_names, _collect_ctor_names,
    SYMBOL_KIND_TO_NODES,
)

# ── AST helpers ───────────────────────────────────────────────────────────────

def _line(node) -> int:
    return node.start_point[0] + 1


def _strip_generic(name: str) -> str:
    idx = name.find("<")
    return name[:idx].strip() if idx >= 0 else name.strip()


def _type_names(type_txt: str) -> set:
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
    for child in node.children:
        if child.type == "variable_declaration":
            t = child.child_by_field_name("type")
            if t:
                return _text(t, src).strip()
    return ""


def _build_sig(node, src) -> str:
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


def _enclosing_type_name(node, src) -> str:
    p = node.parent
    while p:
        if p.type in _TYPE_DECL_NODES:
            nn = p.child_by_field_name("name")
            if nn:
                return _text(nn, src).strip()
        p = p.parent
    return ""


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
    if "." in method_name:
        qualifier, bare_name = method_name.rsplit(".", 1)
    else:
        qualifier, bare_name = None, method_name

    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "invocation_expression"):
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
                    expr_txt = _text(expr, src).strip() if expr else ""
                    if not (expr_txt == qualifier or expr_txt.endswith("." + qualifier)):
                        matched = None
        elif fn.type in ("identifier", "generic_name"):
            if qualifier is None:
                nn = fn.child_by_field_name("name") if fn.type == "generic_name" else fn
                if nn:
                    matched = _strip_generic(_text(nn, src))
        if matched == bare_name:
            raw = _text(node, src).replace("\n", " ").replace("\r", "")
            if len(raw) > 140:
                raw = raw[:140] + "…"
            results.append((_line(node), raw))

    if qualifier is None:
        for node in _find_all(tree.root_node, lambda n: n.type == "object_creation_expression"):
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
    if "." in member_name:
        qualifier, bare_name = member_name.rsplit(".", 1)
    else:
        qualifier, bare_name = None, member_name

    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "member_access_expression"):
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
    results  = []
    seen_rows = set()

    def _is_decl_name(node):
        p = node.parent
        if not p:
            return False
        nn = p.child_by_field_name("name")
        return nn is not None and nn.start_byte == node.start_byte

    def _is_invocation_target(node):
        p = node.parent
        if not p:
            return False
        if p.type == "invocation_expression":
            fn = p.child_by_field_name("function")
            if fn and fn.type == "identifier" and fn.start_byte == node.start_byte:
                return True
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
    kind_nodes = SYMBOL_KIND_TO_NODES.get((symbol_kind or "").lower().strip())
    target_nodes = kind_nodes if kind_nodes is not None else (_TYPE_DECL_NODES | _MEMBER_DECL_NODES)
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type in target_nodes):
        name_node = node.child_by_field_name("name")
        if not name_node or _text(name_node, src).strip() != name:
            continue
        kind      = node.type.replace("_declaration", "").replace("statement", "").replace("_", " ").strip()
        start_row = node.start_point[0]
        end_row   = node.end_point[0]
        body_node = node.child_by_field_name("body")
        if body_node and not include_body:
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
            df_t = ""
            children = p.children
            for idx, ch in enumerate(children):
                if not ch.is_named and _text(ch, src).strip() == "=" and idx + 1 < len(children):
                    df_t = f" = {_text(children[idx + 1], src).strip()}"
                    break
            mods = [_text(c, src) for c in children
                    if c.is_named and c.type in ("modifier", "parameter_modifier")]
            mod_t = " ".join(mods) + " " if mods else ""
            param_lines.append(f"  {mod_t}{pt_t} {pn_t}{df_t}".rstrip())
        results.append((_line(node), "\n".join(param_lines) or "(no parameters)"))
    return results


def _q_field_type(src, tree, lines, type_name):
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
                    cls = _enclosing_type_name(node, src)
                    cls_prefix = f"[in {cls}] " if cls else ""
                    results.append((_line(node), f"{label} {type_txt} {_text(vn, src)}  {cls_prefix}"))
        else:
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
                results.append((_line(node), f"[prop]  {type_txt} {_text(name_node, src)}  {cls_prefix}"))
    return results


def _q_param_type(src, tree, lines, type_name):
    results = []
    for mnode in _find_all(tree.root_node,
                           lambda n: n.type in ("method_declaration", "constructor_declaration",
                                                "local_function_statement", "delegate_declaration",
                                                "lambda_expression")):
        params_node = mnode.child_by_field_name("parameters")
        if not params_node:
            continue
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
            results.append((_line(p), f"[{kind}] {mname}({mod_t}{pt_txt} {pn_txt})"))
    return results


def _q_return_type(src, tree, lines, type_name):
    results = []
    for node in _find_all(tree.root_node,
                          lambda n: n.type in ("method_declaration",
                                               "constructor_declaration",
                                               "local_function_statement")):
        type_node = node.child_by_field_name("type")
        ret_type_txt = _text(type_node, src).strip() if type_node else ""
        if type_name not in _type_names(ret_type_txt):
            continue
        name_node = node.child_by_field_name("name")
        mname = _text(name_node, src).strip() if name_node else "<anonymous>"
        params_node = node.child_by_field_name("parameters")
        param_parts = []
        if params_node:
            for p in _find_all(params_node, lambda n: n.type == "parameter"):
                pt = p.child_by_field_name("type")
                pn = p.child_by_field_name("name")
                pt_t = _text(pt, src).strip() if pt else ""
                pn_t = _text(pn, src).strip() if pn else ""
                param_parts.append(f"{pt_t} {pn_t}".strip())
        sig_text = f"{ret_type_txt} {mname}({', '.join(param_parts)})".strip()
        results.append((_line(node), sig_text))
    return results


def _q_local_type(src, tree, lines, type_name):
    results = []
    for node in _find_all(tree.root_node, lambda n: n.type == "local_declaration_statement"):
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
                results.append((_line(node), f"[local] {type_txt} {_text(vn, src)}  {cls_prefix}"))
    return results


def _q_base_uses(src, tree, lines, type_name):
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
    children = declarator.children
    if len(children) >= 3 and children[1].type == "=":
        return children[2]
    return None


def q_accesses_on(src, tree, lines, type_name):
    var_names   = set()
    array_names = set()

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

    for node in _find_all(tree.root_node, lambda n: n.type == "parameter"):
        pt = node.child_by_field_name("type")
        if not pt:
            continue
        if type_name not in _type_names(_text(pt, src).strip()):
            continue
        pn = node.child_by_field_name("name")
        if pn:
            var_names.add(_text(pn, src).strip())

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
                t = expr.child_by_field_name("right") or expr.child_by_field_name("type")
                if t and type_name in _type_names(_text(t, src)):
                    var_names.add(name)

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

    results = []
    seen_rows = set()
    for node in _find_all(tree.root_node, lambda n: n.type == "member_access_expression"):
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


# ── Process function ──────────────────────────────────────────────────────────

def process_cs_file(path, mode, mode_arg, show_path, count_only, context=0,
                 src_root=None, include_body=False, symbol_kind=None, uses_kind=None):
    import tree_sitter_c_sharp as tscsharp
    from tree_sitter import Language, Parser

    _CS = Language(tscsharp.language())
    _parser = Parser(_CS)

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
        "classes":      lambda: q_classes(src_bytes, tree, lines),
        "methods":      lambda: q_methods(src_bytes, tree, lines),
        "fields":       lambda: q_fields(src_bytes, tree, lines),
        "calls":        lambda: q_calls(src_bytes, tree, lines, mode_arg),
        "implements":   lambda: q_implements(src_bytes, tree, lines, mode_arg),
        "uses":         lambda: q_uses(src_bytes, tree, lines, mode_arg, uses_kind=uses_kind),
        "accesses_on":  lambda: q_accesses_on(src_bytes, tree, lines, mode_arg),
        "all_refs":     lambda: q_all_refs(src_bytes, tree, lines, mode_arg),
        "casts":        lambda: q_casts(src_bytes, tree, lines, mode_arg),
        "attrs":        lambda: q_attrs(src_bytes, tree, lines, mode_arg),
        "accesses_of":  lambda: q_accesses_of(src_bytes, tree, lines, mode_arg),
        "usings":       lambda: q_usings(src_bytes, tree, lines),
        "declarations": lambda: q_declarations(src_bytes, tree, lines, mode_arg,
                                               include_body=include_body, symbol_kind=symbol_kind),
        "params":       lambda: q_params(src_bytes, tree, lines, mode_arg),
    }

    fn = dispatch.get(mode)
    if not fn:
        return 0

    results = fn()
    if not results:
        return 0

    return _print_results(results, path, lines, show_path, count_only, context, src_root, mode)


def _print_results(results, path, lines, show_path, count_only, context, src_root, mode):
    import os
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

    for line_num_str, text in results:
        if show_path:
            print(f"{_disp_base}:{line_num_str}: {text}")
        else:
            print(f"{line_num_str}: {text}")

        if context > 0 and mode != "declarations":
            try:
                row = int(line_num_str) - 1
                start = max(0, row - context)
                end   = min(len(lines), row + context + 1)
                for i, ln in enumerate(lines[start:end], start):
                    if i == row:
                        continue
                    prefix = f"  {_disp_base}:{i + 1}-" if show_path else f"  {i + 1}-"
                    print(f"{prefix} {ln}")
                print()
            except (ValueError, IndexError):
                pass
    return len(results)
