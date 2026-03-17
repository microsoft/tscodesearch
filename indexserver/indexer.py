"""
Index source files into Typesense.
Uses tree-sitter to extract class/interface/method/property symbols.

Usage:
    python indexer.py [--resethard]
    python indexer.py --src /path/to/src --collection my_collection --resethard
"""

import os
import re
import sys
import time
import hashlib
import argparse

# Allow running as a standalone script: add claudeskills/ to path
_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

import typesense
import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

try:
    import tree_sitter_python as tspython
    _PY_AVAILABLE = True
except ImportError:
    _PY_AVAILABLE = False

from indexserver.config import (
    TYPESENSE_CLIENT_CONFIG, COLLECTION, SRC_ROOT,
    INCLUDE_EXTENSIONS, EXCLUDE_DIRS, MAX_FILE_BYTES,
    collection_for_root,
)
from cs_ast import (
    _TYPE_DECL_NODES, _MEMBER_DECL_NODES, _QUALIFIED_RE,
    _find_all, _text, _unqualify, _unqualify_type,
    _base_type_names, _collect_ctor_names,
)
_node_text = _text  # local alias — indexer historically used _node_text

def _to_native_path(path: str) -> str:
    """Convert a Windows-style path (C:/foo or C:\\foo) to the platform-native form.

    On WSL (Linux), converts to /mnt/c/foo so that open() works correctly.
    On Windows, converts forward slashes to backslashes.
    """
    p = path.replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        if os.sep == "/":
            # WSL: C:/foo/bar → /mnt/c/foo/bar
            return "/mnt/" + p[0].lower() + p[2:]
        else:
            # Windows: C:/foo/bar → C:\foo\bar
            return p.replace("/", "\\")
    return p


_SRC_ROOT_NATIVE = _to_native_path(SRC_ROOT)

CS = Language(tscsharp.language())
_parser = Parser(CS)

if _PY_AVAILABLE:
    _PY = Language(tspython.language())
    _py_parser = Parser(_PY)
else:
    _py_parser = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_FIELDS = [
    {"name": "id",            "type": "string"},
    {"name": "relative_path", "type": "string"},
    {"name": "filename",      "type": "string"},
    {"name": "extension",     "type": "string", "facet": True},
    {"name": "subsystem",     "type": "string", "facet": True},
    {"name": "namespace",     "type": "string", "optional": True},
    {"name": "class_names",   "type": "string[]", "optional": True},
    {"name": "method_names",  "type": "string[]", "optional": True},
    {"name": "symbols",       "type": "string[]"},
    {"name": "content",       "type": "string"},
    {"name": "mtime",         "type": "int64"},
    # Tier 1 semantic fields
    {"name": "base_types",    "type": "string[]", "optional": True},
    {"name": "call_sites",    "type": "string[]", "optional": True},
    {"name": "method_sigs",   "type": "string[]", "optional": True},
    {"name": "cast_sites",    "type": "string[]", "optional": True},
    # Tier 2 semantic fields
    {"name": "type_refs",     "type": "string[]", "optional": True},
    {"name": "attributes",    "type": "string[]", "optional": True, "facet": True},
    {"name": "usings",        "type": "string[]", "optional": True},
    {"name": "priority",      "type": "int32"},
    # Tier 1 expanded fields
    {"name": "return_types",  "type": "string[]", "optional": True},
    {"name": "param_types",   "type": "string[]", "optional": True},
]


def build_schema(collection_name: str) -> dict:
    return {
        "name": collection_name,
        "fields": _SCHEMA_FIELDS,
        # Split tokens on C# syntax characters so that parameter types and
        # generic type arguments are individually searchable.
        # e.g. "Task<Widget> GetAsync(int id)"  →  Task  Widget  GetAsync  int  id
        # Requires ts index --resethard to recreate the collection with the new schema.
        "token_separators": ["(", ")", "<", ">", "[", "]", ",", ".",",","+","-","/","*","?"],
    }


SCHEMA = build_schema(COLLECTION)


# ---------------------------------------------------------------------------
# Tree-sitter symbol extraction
# ---------------------------------------------------------------------------

def _dedupe(seq):
    seen = set()
    out = []
    for x in seq:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _expand_type_refs(text: str) -> list:
    """Return the unqualified type string PLUS each individual type name it contains.

    This ensures that searching for a type finds it whether it appears as the
    direct type or as a type argument of a generic wrapper:
      'IList<IFoo>'               → ['IList<IFoo>', 'IList', 'IFoo']
      'Task<IBlobStore>'          → ['Task<IBlobStore>', 'Task', 'IBlobStore']
      'Dictionary<string, IFoo>' → ['Dictionary<string, IFoo>', 'Dictionary', 'string', 'IFoo']
      'IBlobStore'                → ['IBlobStore']
    """
    unqual = _unqualify_type(text)
    names = [unqual]
    for name in re.findall(r'[A-Za-z_]\w*', unqual):
        if name != unqual:
            names.append(name)
    return names


def extract_cs_metadata(src_bytes: bytes) -> dict:
    """Extract rich C# metadata for tier 1+2 semantic indexing."""
    try:
        tree = _parser.parse(src_bytes)
    except Exception:
        return {
            "namespace": "", "class_names": [], "method_names": [],
            "base_types": [], "call_sites": [], "cast_sites": [], "method_sigs": [],
            "type_refs": [], "attributes": [], "usings": [],
            "return_types": [], "param_types": [],
        }

    root = tree.root_node
    namespace = ""
    class_names = []
    method_names = []
    base_types = []
    call_sites = []
    cast_sites = []
    method_sigs = []
    type_refs = []
    attributes = []
    usings = []
    return_types = []
    param_types = []

    # Namespace
    ns_nodes = _find_all(root, lambda n: n.type in (
        "namespace_declaration", "file_scoped_namespace_declaration"
    ))
    if ns_nodes:
        name_node = ns_nodes[0].child_by_field_name("name")
        if name_node:
            namespace = _node_text(name_node, src_bytes)

    # T2: using imports
    for node in _find_all(root, lambda n: n.type == "using_directive"):
        for child in node.named_children:
            if child.type in ("identifier", "qualified_name"):
                text = _node_text(child, src_bytes)
                usings.append(text.split(".")[0])  # top-level namespace
                break

    # T2: attributes
    for node in _find_all(root, lambda n: n.type == "attribute"):
        name_node = node.child_by_field_name("name")
        if name_node:
            attr_name = _node_text(name_node, src_bytes)
            if attr_name.endswith("Attribute"):
                attr_name = attr_name[:-len("Attribute")]
            attributes.append(_unqualify(attr_name))

    # Type declarations
    for node in _find_all(root, lambda n: n.type in _TYPE_DECL_NODES):
        name_node = node.child_by_field_name("name")
        if name_node:
            class_names.append(_node_text(name_node, src_bytes))

        # T1: base_types
        for bt in _base_type_names(node, src_bytes):
            unqual = _unqualify(bt)
            # Strip generic suffix: 'IBar<C>' → 'IBar'
            idx = unqual.find("<")
            base_types.append(unqual[:idx].strip() if idx >= 0 else unqual)

    # Member declarations
    for node in _find_all(root, lambda n: n.type in _MEMBER_DECL_NODES):
        name_node = node.child_by_field_name("name")
        if name_node:
            method_names.append(_node_text(name_node, src_bytes))
        elif node.type in ("field_declaration", "event_field_declaration"):
            for var in _find_all(node, lambda n: n.type == "variable_declarator"):
                vname = var.child_by_field_name("name")
                if vname:
                    method_names.append(_node_text(vname, src_bytes))

        # T1: method signatures (methods + constructors)
        if node.type in ("method_declaration", "local_function_statement",
                         "constructor_declaration"):
            ret_node = node.child_by_field_name("returns") or node.child_by_field_name("type")
            name_node2 = node.child_by_field_name("name")
            params_node = node.child_by_field_name("parameters")
            if name_node2 and params_node:
                ret_txt = _node_text(ret_node, src_bytes).strip() if ret_node else ""
                mname = _node_text(name_node2, src_bytes)
                sig_param_types = []
                for param in _find_all(params_node, lambda n: n.type == "parameter"):
                    ptype = param.child_by_field_name("type")
                    if ptype:
                        ptype_txt = _node_text(ptype, src_bytes).strip()
                        sig_param_types.append(ptype_txt)
                        param_types.extend(_expand_type_refs(ptype_txt))
                sig = f"{ret_txt} {mname}({', '.join(sig_param_types)})".strip()
                method_sigs.append(sig)
                if ret_txt:
                    return_types.extend(_expand_type_refs(ret_txt))

        # T2: type_refs
        if node.type in ("field_declaration", "event_field_declaration"):
            # type lives inside the variable_declaration child
            var_decl = next((c for c in node.children if c.type == "variable_declaration"), None)
            if var_decl:
                type_node = var_decl.child_by_field_name("type")
                if type_node:
                    type_refs.extend(_expand_type_refs(_node_text(type_node, src_bytes).strip()))
        elif node.type in ("property_declaration", "event_declaration"):
            type_node = node.child_by_field_name("type")
            if type_node:
                type_refs.extend(_expand_type_refs(_node_text(type_node, src_bytes).strip()))
        if node.type == "method_declaration":
            ret_node = node.child_by_field_name("returns") or node.child_by_field_name("type")
            if ret_node:
                type_refs.extend(_expand_type_refs(_node_text(ret_node, src_bytes).strip()))
        if node.type in ("method_declaration", "constructor_declaration"):
            params_node = node.child_by_field_name("parameters")
            if params_node:
                for param in _find_all(params_node, lambda n: n.type == "parameter"):
                    ptype = param.child_by_field_name("type")
                    if ptype:
                        type_refs.extend(_expand_type_refs(_node_text(ptype, src_bytes).strip()))

    # T1: call sites (method calls)
    for node in _find_all(root, lambda n: n.type == "invocation_expression"):
        fn_node = node.child_by_field_name("function")
        if fn_node:
            if fn_node.type == "member_access_expression":
                name_node = fn_node.child_by_field_name("name")
                if name_node:
                    call_sites.append(_node_text(name_node, src_bytes))
            elif fn_node.type == "identifier":
                call_sites.append(_node_text(fn_node, src_bytes))

    # T1: call sites (constructor calls — new Foo(...))
    call_sites.extend(_collect_ctor_names(root, src_bytes))

    # T2: local variable declaration types (BlobStore store = ...; inside methods)
    for node in _find_all(root, lambda n: n.type == "local_declaration_statement"):
        var_decl = next((c for c in node.children if c.type == "variable_declaration"), None)
        if var_decl:
            type_node = var_decl.child_by_field_name("type")
            if type_node:
                type_refs.extend(_expand_type_refs(_node_text(type_node, src_bytes).strip()))

    # T2: static call receivers — PascalCase identifier as receiver of .Method(...)
    # e.g. BlobStore.Delete(key) → 'BlobStore' added to type_refs
    for node in _find_all(root, lambda n: n.type == "invocation_expression"):
        fn_node = node.child_by_field_name("function")
        if fn_node and fn_node.type == "member_access_expression":
            expr = fn_node.child_by_field_name("expression")
            if expr and expr.type == "identifier":
                name = _node_text(expr, src_bytes)
                if name and name[0].isupper():
                    type_refs.extend(_expand_type_refs(name))

    # T1: explicit cast target types — (BlobStore)obj → 'BlobStore' in cast_sites
    for node in _find_all(root, lambda n: n.type == "cast_expression"):
        type_node = node.child_by_field_name("type")
        if type_node:
            cast_sites.extend(_expand_type_refs(_node_text(type_node, src_bytes).strip()))

    # base_types are also type_refs: if you implement IFoo, you're "using" IFoo
    type_refs.extend(base_types)

    return {
        "namespace":    namespace,
        "class_names":  _dedupe(class_names),
        "method_names": _dedupe(method_names),
        "base_types":   _dedupe(base_types),
        "call_sites":   _dedupe(call_sites),
        "cast_sites":   _dedupe(cast_sites),
        "method_sigs":  _dedupe(method_sigs),
        "type_refs":    _dedupe(type_refs),
        "attributes":   _dedupe(attributes),
        "usings":       _dedupe(usings),
        "return_types": _dedupe(return_types),
        "param_types":  _dedupe(param_types),
    }


def extract_py_metadata(src_bytes: bytes) -> dict:
    """Extract Python metadata for tier 1+2 semantic indexing."""
    _empty = {
        "namespace": "", "class_names": [], "method_names": [],
        "base_types": [], "call_sites": [], "cast_sites": [], "method_sigs": [],
        "type_refs": [], "attributes": [], "usings": [],
        "return_types": [], "param_types": [],
    }
    if not _PY_AVAILABLE or _py_parser is None:
        return _empty
    try:
        tree = _py_parser.parse(src_bytes)
    except Exception:
        return _empty

    root = tree.root_node
    class_names = []
    method_names = []
    base_types = []
    call_sites = []
    method_sigs = []
    type_refs = []
    attributes = []
    usings = []
    py_return_types = []
    py_param_types = []

    # Classes and base types
    for node in _find_all(root, lambda n: n.type == "class_definition"):
        name_node = node.child_by_field_name("name")
        if name_node:
            class_names.append(_node_text(name_node, src_bytes))
        superclasses = node.child_by_field_name("superclasses")
        if superclasses:
            for child in superclasses.named_children:
                if child.type == "identifier":
                    base_types.append(_node_text(child, src_bytes))
                elif child.type == "attribute":
                    attr = child.child_by_field_name("attribute")
                    if attr:
                        base_types.append(_node_text(attr, src_bytes))

    # Functions/methods — names, signatures, type refs
    for node in _find_all(root, lambda n: n.type == "function_definition"):
        name_node = node.child_by_field_name("name")
        if name_node:
            method_names.append(_node_text(name_node, src_bytes))
        params_node = node.child_by_field_name("parameters")
        return_node = node.child_by_field_name("return_type")
        if name_node and params_node:
            mname = _node_text(name_node, src_bytes)
            params_txt = _node_text(params_node, src_bytes)
            ret_txt = _node_text(return_node, src_bytes).strip() if return_node else ""
            sig = f"def {mname}{params_txt}"
            if ret_txt:
                sig += f" -> {ret_txt}"
            method_sigs.append(sig)
        if return_node:
            ret_type_txt = _node_text(return_node, src_bytes).strip()
            type_refs.extend(_expand_type_refs(ret_type_txt))
            py_return_types.extend(_expand_type_refs(ret_type_txt))
        if params_node:
            for param in params_node.named_children:
                if param.type in ("typed_parameter", "typed_default_parameter"):
                    ptype = param.child_by_field_name("type")
                    if ptype:
                        ptype_txt = _node_text(ptype, src_bytes).strip()
                        type_refs.extend(_expand_type_refs(ptype_txt))
                        py_param_types.extend(_expand_type_refs(ptype_txt))

    # Decorators (stored in "attributes" field for consistency)
    for node in _find_all(root, lambda n: n.type == "decorator"):
        full_text = _node_text(node, src_bytes).strip().lstrip("@")
        dname = full_text.split("(")[0].split(".")[-1].strip()
        if dname:
            attributes.append(dname)

    # Call sites
    for node in _find_all(root, lambda n: n.type == "call"):
        fn = node.child_by_field_name("function")
        if fn:
            if fn.type == "identifier":
                call_sites.append(_node_text(fn, src_bytes))
            elif fn.type == "attribute":
                attr = fn.child_by_field_name("attribute")
                if attr:
                    call_sites.append(_node_text(attr, src_bytes))

    # Imports (stored in "usings" field for consistency)
    for node in _find_all(root, lambda n: n.type == "import_statement"):
        for child in node.named_children:
            if child.type == "dotted_name":
                usings.append(_node_text(child, src_bytes).split(".")[0])
            elif child.type == "aliased_import" and child.named_children:
                usings.append(_node_text(child.named_children[0], src_bytes).split(".")[0])

    for node in _find_all(root, lambda n: n.type == "import_from_statement"):
        module_node = node.child_by_field_name("module_name")
        if module_node:
            usings.append(_node_text(module_node, src_bytes).lstrip(".").split(".")[0])

    return {
        "namespace":    "",
        "class_names":  _dedupe(class_names),
        "method_names": _dedupe(method_names),
        "base_types":   _dedupe(base_types),
        "call_sites":   _dedupe(call_sites),
        "cast_sites":   [],   # Python has no explicit cast syntax
        "method_sigs":  _dedupe(method_sigs),
        "type_refs":    _dedupe(type_refs),
        "attributes":   _dedupe(attributes),
        "usings":       _dedupe(usings),
        "return_types": _dedupe(py_return_types),
        "param_types":  _dedupe(py_param_types),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def file_id(relative_path: str) -> str:
    return hashlib.md5(relative_path.replace("\\", "/").encode()).hexdigest()


def subsystem_from_path(relative_path: str) -> str:
    parts = relative_path.replace("\\", "/").split("/")
    return parts[0] if parts else ""


def should_skip_dir(dirname: str) -> bool:
    return dirname in EXCLUDE_DIRS or dirname.startswith(".")


_PRIORITY = {
    ".cs":   3,
    ".h": 2, ".hpp": 2, ".cpp": 2, ".c": 2, ".idl": 2,
    ".py": 1, ".ts": 1, ".js": 1, ".ps1": 1, ".sh": 1, ".cmd": 1, ".bat": 1,
}


def _file_priority(ext: str) -> int:
    return _PRIORITY.get(ext, 0)


def build_document(full_path: str, relative_path: str) -> dict:
    try:
        stat = os.stat(full_path)
        src_bytes = open(full_path, "rb").read()
    except OSError:
        return None

    ext = os.path.splitext(full_path)[1].lower()
    if ext == ".cs":
        meta = extract_cs_metadata(src_bytes)
    elif ext == ".py" and _PY_AVAILABLE:
        meta = extract_py_metadata(src_bytes)
    else:
        meta = {
            "namespace": "", "class_names": [], "method_names": [],
            "base_types": [], "call_sites": [], "cast_sites": [], "method_sigs": [],
            "type_refs": [], "attributes": [], "usings": [],
            "return_types": [], "param_types": [],
        }

    symbols = list(dict.fromkeys(meta["class_names"] + meta["method_names"]))
    # Store only unique identifier tokens — keeps the index small while
    # preserving full recall for word-level search (we never phrase-search content).
    _raw = src_bytes.decode("utf-8", errors="replace")
    content = " ".join(dict.fromkeys(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', _raw)))
    relative_path_norm = relative_path.replace("\\", "/")

    return {
        "id":            file_id(relative_path_norm),
        "relative_path": relative_path_norm,
        "filename":      os.path.basename(full_path),
        "extension":     ext.lstrip("."),
        "subsystem":     subsystem_from_path(relative_path),
        "namespace":     meta["namespace"],
        "class_names":   meta["class_names"],
        "method_names":  meta["method_names"],
        "symbols":       symbols if symbols else [""],
        "content":       content,
        "mtime":         int(stat.st_mtime),
        "priority":      _file_priority(ext),
        "base_types":    meta["base_types"],
        "call_sites":    meta["call_sites"],
        "cast_sites":    meta["cast_sites"],
        "method_sigs":   meta["method_sigs"],
        "type_refs":     meta["type_refs"],
        "attributes":    meta["attributes"],
        "usings":        meta["usings"],
        "return_types":  meta["return_types"],
        "param_types":   meta["param_types"],
    }


# ---------------------------------------------------------------------------
# Collection management
# ---------------------------------------------------------------------------

def get_client():
    return typesense.Client(TYPESENSE_CLIENT_CONFIG)


# ---------------------------------------------------------------------------
# Schema verification
# ---------------------------------------------------------------------------

_EXPECTED_TOKEN_SEPARATORS = set(build_schema("_")["token_separators"])

def verify_schema(client, collection: str) -> tuple[bool, list[str]]:
    """Check a Typesense collection against the expected schema.

    Returns (exists, warnings):
      exists=False — collection not found (not yet indexed); warnings is empty.
      exists=True  — collection found; warnings lists any field/type mismatches.
    Does not raise; callers should log the warnings.
    """
    try:
        info = client.collections[collection].retrieve()
    except Exception as e:
        err_str = str(e).lower()
        if "404" in err_str or "not found" in err_str:
            return False, []   # collection simply doesn't exist yet
        return False, [f"could not retrieve collection {collection!r}: {e}"]

    warnings = []

    # ── field checks ──────────────────────────────────────────────────────────
    # Typesense treats 'id' as a built-in field and never returns it in the
    # collection's fields list — skip it to avoid a spurious warning.
    actual_fields = {f["name"]: f for f in info.get("fields", [])}
    for expected in _SCHEMA_FIELDS:
        name = expected["name"]
        if name == "id":
            continue
        if name not in actual_fields:
            warnings.append(f"field {name!r} missing from collection")
            continue
        actual = actual_fields[name]
        if actual.get("type") != expected.get("type"):
            warnings.append(
                f"field {name!r} type: expected {expected['type']!r}, "
                f"got {actual.get('type')!r}"
            )
        if bool(expected.get("facet")) != bool(actual.get("facet")):
            warnings.append(
                f"field {name!r} facet: expected {expected.get('facet', False)}, "
                f"got {actual.get('facet', False)}"
            )

    # ── token_separators check ────────────────────────────────────────────────
    actual_seps = set(info.get("token_separators", []))
    missing_seps = _EXPECTED_TOKEN_SEPARATORS - actual_seps
    extra_seps   = actual_seps - _EXPECTED_TOKEN_SEPARATORS
    if missing_seps:
        warnings.append(f"token_separators missing: {sorted(missing_seps)}")
    if extra_seps:
        warnings.append(f"token_separators unexpected: {sorted(extra_seps)}")

    return True, warnings


def verify_all_schemas(client) -> dict:
    """Verify schema for every configured root; print results to stdout.

    Returns a dict keyed by root name:
        {"ok": bool, "warnings": [str, ...], "collection": str}
    """
    from indexserver.config import ROOTS, collection_for_root
    results = {}
    for name in ROOTS:
        coll = collection_for_root(name)
        exists, warnings = verify_schema(client, coll)
        results[name] = {
            "ok":                 exists and not warnings,
            "collection_exists":  exists,
            "warnings":           warnings,
            "collection":         coll,
        }
        if not exists:
            print(f"[schema] MISSING {coll} (not yet indexed)", flush=True)
        elif warnings:
            for w in warnings:
                print(f"[schema] WARN  {coll}: {w}", flush=True)
        else:
            print(f"[schema] OK    {coll}", flush=True)
    return results


def ensure_collection(client, resethard=False, collection=None):
    coll_name = collection or COLLECTION
    schema = build_schema(coll_name)

    # Typesense can return 503 "Not Ready" briefly after startup even after
    # /health reports OK.  After a hard reset the server may also refuse
    # connections until fully initialized.  Retry on any transient error.
    exists = True
    for attempt in range(8):
        try:
            client.collections[coll_name].retrieve()
            break
        except Exception as e:
            err_str = str(e).lower()
            is_transient = (
                "503" in err_str
                or "connection" in err_str
                or "timeout" in err_str
                or "not ready" in err_str
            )
            if is_transient and attempt < 7:
                print(f"  Typesense not ready yet (attempt {attempt + 1}/8), retrying in 5s...")
                time.sleep(5)
            else:
                exists = False
                break

    if exists and resethard:
        print(f"Dropping existing collection '{coll_name}'...")
        client.collections[coll_name].delete()
        exists = False

    if not exists:
        print(f"Creating collection '{coll_name}'...")
        client.collections.create(schema)
        print("Collection created.")
    else:
        print(f"Collection '{coll_name}' already exists.")


# ---------------------------------------------------------------------------
# Full index walk
# ---------------------------------------------------------------------------

def walk_source_files(src_root: str):
    """Yield (full_path, relative_path) for all source files, respecting .gitignore."""
    import pathspec

    src_root = _to_native_path(src_root)

    # Cache: abs_dir -> PathSpec | None
    _spec_cache: dict = {}

    def _load_spec(dirpath: str):
        if dirpath in _spec_cache:
            return _spec_cache[dirpath]
        gi = os.path.join(dirpath, ".gitignore")
        spec = None
        if os.path.isfile(gi):
            try:
                with open(gi, "r", encoding="utf-8", errors="replace") as f:
                    spec = pathspec.PathSpec.from_lines("gitwildmatch", f)
            except OSError:
                pass
        _spec_cache[dirpath] = spec
        return spec

    def _is_ignored(full_path: str) -> bool:
        """Check all ancestor .gitignore files from src_root down to the item's parent."""
        rel_parts = os.path.relpath(full_path, src_root).replace("\\", "/").split("/")
        check_dir = src_root
        for i, part in enumerate(rel_parts):
            spec = _load_spec(check_dir)
            if spec and spec.match_file("/".join(rel_parts[i:])):
                return True
            if i < len(rel_parts) - 1:
                check_dir = os.path.join(check_dir, part)
        return False

    for dirpath, dirs, files in os.walk(src_root, topdown=True):
        dirs[:] = [
            d for d in dirs
            if not should_skip_dir(d)
            and not _is_ignored(os.path.join(dirpath, d))
        ]
        for filename in files:
            full_path = os.path.join(dirpath, filename)
            ext = os.path.splitext(filename)[1].lower()
            if ext not in INCLUDE_EXTENSIONS:
                continue
            try:
                if os.path.getsize(full_path) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            if _is_ignored(full_path):
                continue
            rel = os.path.relpath(full_path, src_root).replace("\\", "/")
            yield full_path, rel


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s"


def _flush(client, docs, verbose, collection=None):
    coll_name = collection or COLLECTION
    try:
        results = client.collections[coll_name].documents.import_(
            docs, {"action": "upsert"}
        )
        if verbose:
            failed = [r for r in results if not r.get("success")]
            for f in failed:
                print(f"  WARN: {f}")
    except Exception as e:
        print(f"  ERROR during batch import: {e}")


def index_file_list(
    client,
    file_pairs,
    coll_name: str,
    batch_size: int = 50,
    verbose: bool = False,
    on_progress=None,
    stop_event=None,
) -> tuple[int, int]:
    """Shared batch-upsert pipeline used by both the full indexer and the verifier.

    Args:
        client:      Typesense client.
        file_pairs:  Iterable of (full_path, relative_path) tuples.
        coll_name:   Typesense collection name.
        batch_size:  Documents per import batch.
        verbose:     Print per-document warnings on import failure.
        on_progress: Optional callable(n_indexed: int, n_errors: int) invoked
                     after every flushed batch.
        stop_event:  Optional threading.Event; when set the pipeline flushes
                     the current batch and returns early.

    Returns:
        (total_indexed, total_errors)
    """
    docs_batch: list[dict] = []
    total = 0
    errors = 0

    for full_path, rel in file_pairs:
        if stop_event and stop_event.is_set():
            break

        doc = build_document(full_path, rel)
        if doc is None:
            errors += 1
            continue

        docs_batch.append(doc)

        if len(docs_batch) >= batch_size:
            _flush(client, docs_batch, verbose, coll_name)
            total += len(docs_batch)
            docs_batch = []
            if on_progress:
                on_progress(total, errors)

    if docs_batch:
        _flush(client, docs_batch, verbose, coll_name)
        total += len(docs_batch)
        if on_progress:
            on_progress(total, errors)

    return total, errors


def walk_and_enqueue(
    src_root: str,
    collection: str,
    queue,
    resethard: bool = False,
    stop_event=None,
) -> tuple[int, int]:
    """Walk *src_root* and feed every source file into *queue*.

    Calls ensure_collection() first (dropping the collection when resethard=True).
    Returns (new_entries, deduped_entries).
    """
    src_root = _to_native_path(src_root)
    client = get_client()
    ensure_collection(client, resethard=resethard, collection=collection)
    return queue.enqueue_bulk(
        walk_source_files(src_root),
        collection=collection,
        stop_event=stop_event,
    )


def run_index(src_root=None, resethard=False, batch_size=50, verbose=False, collection=None):
    if src_root is None:
        src_root = _SRC_ROOT_NATIVE
    else:
        src_root = _to_native_path(src_root)
    coll_name = collection or COLLECTION
    client = get_client()
    ensure_collection(client, resethard=resethard, collection=coll_name)

    t0 = time.time()
    last_report_t = t0
    last_report_n = 0
    current_sub = ""
    total_indexed = 0
    total_errors  = 0

    print(f"Indexing source files under: {src_root}")
    print(f"Extensions: {', '.join(sorted(INCLUDE_EXTENSIONS))}")
    print()

    def _tracked_files():
        """Yield (full_path, rel) from walk_source_files with subsystem logging."""
        nonlocal current_sub
        for full_path, rel in walk_source_files(src_root):
            sub = subsystem_from_path(rel)
            if sub != current_sub:
                current_sub = sub
                elapsed = time.time() - t0
                print(f"  [{_fmt_time(elapsed)}] subsystem: {sub}  "
                      f"(total so far: {total_indexed})")
            yield full_path, rel

    def _rate_report(n: int, errs: int) -> None:
        nonlocal last_report_t, last_report_n, total_indexed, total_errors
        total_indexed = n
        total_errors  = errs
        now = time.time()
        if now - last_report_t >= 30:
            elapsed  = now - t0
            delta_n  = n - last_report_n
            delta_t  = now - last_report_t
            rate     = delta_n / delta_t if delta_t > 0 else 0
            print(f"  [{_fmt_time(elapsed)}] {n:,} files indexed  "
                  f"({rate:.0f} files/s)  errors={errs}")
            last_report_t = now
            last_report_n = n

    total_indexed, total_errors = index_file_list(
        client, _tracked_files(), coll_name,
        batch_size=batch_size, verbose=verbose,
        on_progress=_rate_report,
    )

    elapsed = time.time() - t0
    rate = total_indexed / elapsed if elapsed > 0 else 0
    print()
    print(f"Done in {_fmt_time(elapsed)}. "
          f"Indexed {total_indexed:,} files  ({rate:.0f} files/s)  "
          f"errors={total_errors}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Index source files into Typesense")
    ap.add_argument("--resethard", action="store_true",
                    help="Drop and recreate the collection first")
    ap.add_argument("--src", default=_SRC_ROOT_NATIVE,
                    help=f"Root directory to index (default: {_SRC_ROOT_NATIVE})")
    ap.add_argument("--collection", default=None,
                    help="Collection name (default: from config)")
    ap.add_argument("--status", action="store_true",
                    help="Show index stats and exit")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    coll = args.collection or COLLECTION
    if args.status:
        client = get_client()
        try:
            info = client.collections[coll].retrieve()
            n = info.get("num_documents", "?")
            print(f"Collection '{coll}': {n:,} documents indexed")
        except Exception as e:
            print(f"Cannot retrieve index stats: {e}")
    else:
        run_index(src_root=args.src, resethard=args.resethard, verbose=args.verbose,
                  collection=coll)
