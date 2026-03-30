"""
Shared SQL tree-sitter AST helpers.  Used by indexer.py (pre-filter
index building) for semantic extraction from .sql files.

All functions operate on already-parsed tree-sitter nodes — no parser
state here.
"""

from .cs import _find_all, _text   # shared traversal helpers

# ── Node type sets ─────────────────────────────────────────────────────────────

_SQL_LITERAL_NODES = {"comment", "marginalia", "string"}

_TYPE_DECL_NODES = {"create_table", "create_view", "alter_table"}

_FUNCTION_DECL_NODES = {"create_function"}

_PROC_DECL_NODES = {"create_procedure"}

# ── Basic helpers ──────────────────────────────────────────────────────────────

def _line(node) -> int:
    return node.start_point[0] + 1


def _sql_in_literal(node) -> bool:
    p = node.parent
    while p:
        if p.type in _SQL_LITERAL_NODES:
            return True
        p = p.parent
    return False


def _object_name(node, src: bytes) -> str:
    """Extract the unqualified name from an object_reference node
    (e.g. 'dbo.Users' → 'Users', 'Users' → 'Users')."""
    if node is None:
        return ""
    if node.type == "object_reference":
        # Last identifier child is the unqualified name
        idents = [c for c in node.children if c.type == "identifier"]
        if idents:
            return _text(idents[-1], src).strip()
    if node.type == "identifier":
        return _text(node, src).strip()
    return _text(node, src).strip()


def _schema_name(node, src: bytes) -> str:
    """Extract schema prefix from object_reference ('dbo.Users' → 'dbo')."""
    if node is None or node.type != "object_reference":
        return ""
    idents = [c for c in node.children if c.type == "identifier"]
    if len(idents) >= 2:
        return _text(idents[0], src).strip()
    return ""


def _full_object_name(node, src: bytes) -> str:
    """Return schema.name if schema exists, else just name."""
    schema = _schema_name(node, src)
    name = _object_name(node, src)
    if schema and name:
        return f"{schema}.{name}"
    return name


# ── Extraction helpers ─────────────────────────────────────────────────────────

def extract_table_names(root, src: bytes) -> list:
    """Find all table/view names from CREATE TABLE / CREATE VIEW / ALTER TABLE."""
    names = []
    for node in _find_all(root, lambda n: n.type in _TYPE_DECL_NODES):
        ref = next((c for c in node.children if c.type == "object_reference"), None)
        name = _full_object_name(ref, src)
        if name:
            names.append(name)
    return names


def extract_function_names(root, src: bytes) -> list:
    """Find all function names from CREATE FUNCTION."""
    names = []
    for node in _find_all(root, lambda n: n.type in _FUNCTION_DECL_NODES):
        ref = next((c for c in node.children if c.type == "object_reference"), None)
        name = _full_object_name(ref, src)
        if name:
            names.append(name)
    return names


def extract_proc_names_ast(root, src: bytes) -> list:
    """Extract stored procedure names from CREATE PROCEDURE AST nodes."""
    names = []
    for node in _find_all(root, lambda n: n.type in _PROC_DECL_NODES):
        ref = next((c for c in node.children if c.type == "object_reference"), None)
        name = _full_object_name(ref, src)
        if name:
            names.append(name)
    return names


def extract_proc_sigs(root, src: bytes) -> list:
    """Extract procedure signatures as 'ProcName(@param TYPE, ...)' strings."""
    sigs = []
    for node in _find_all(root, lambda n: n.type in _PROC_DECL_NODES):
        ref = next((c for c in node.children if c.type == "object_reference"), None)
        name = _full_object_name(ref, src)
        args = next((c for c in node.children if c.type == "function_arguments"), None)
        if name:
            args_text = _text(args, src).strip() if args else "()"
            sigs.append(f"{name}{args_text}")
    return sigs


def extract_proc_body_refs(root, src: bytes) -> list:
    """Extract table names referenced inside procedure bodies.
    Returns list of (proc_name, table_name) tuples."""
    refs = []
    for node in _find_all(root, lambda n: n.type in _PROC_DECL_NODES):
        proc_ref = next((c for c in node.children if c.type == "object_reference"), None)
        proc_name = _full_object_name(proc_ref, src)
        body = next((c for c in node.children if c.type == "procedure_body"), None)
        if not body or not proc_name:
            continue
        # Find all table references inside the procedure body
        for table_ref in _find_all(body, lambda n: n.type == "object_reference"):
            table_name = _full_object_name(table_ref, src)
            if table_name and table_name != proc_name:
                refs.append((proc_name, table_name))
    return refs


def extract_proc_names_regex(src: bytes) -> list:
    """Regex fallback for T-SQL stored procedures (tree-sitter-sql
    doesn't parse CREATE PROCEDURE reliably)."""
    import re
    text = src.decode("utf-8", errors="replace")
    # Match CREATE [OR ALTER] PROC[EDURE] [schema.]name
    pattern = r'(?i)\bCREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\s+(\[?[\w.]+\]?\.)?(\[?[\w]+\]?)'
    names = []
    for m in re.finditer(pattern, text):
        schema = (m.group(1) or "").strip().rstrip(".").strip("[]")
        name = m.group(2).strip("[]")
        if schema:
            names.append(f"{schema}.{name}")
        else:
            names.append(name)
    return names


_COL_TYPE_NODE_TYPES = {
    "int", "bigint", "smallint", "tinyint", "bit",
    "nvarchar", "varchar", "char", "nchar",
    "datetime", "datetime2", "date", "time", "datetimeoffset",
    "decimal", "numeric", "float", "real", "money",
    "uniqueidentifier", "varbinary", "binary", "image",
    "text", "ntext", "xml", "sql_variant",
}


def _col_type_text(name_node, children, src: bytes) -> str:
    """Return the type text for a column_definition's named children.
    Handles both keyword type nodes and object_reference types (e.g. UNIQUEIDENTIFIER)."""
    past_name = False
    for child in children:
        if child is name_node:
            past_name = True
            continue
        if not past_name:
            continue
        ctype = child.type.lower()
        if ctype in _COL_TYPE_NODE_TYPES or ctype.startswith("keyword_"):
            return _text(child, src).strip().upper()
        # UNIQUEIDENTIFIER and other user-defined types land as object_reference
        if ctype == "object_reference":
            return _text(child, src).strip().upper()
    return ""


def extract_column_info(root, src: bytes) -> tuple:
    """Extract column names and types from column_definition nodes.
    Returns (column_names: list, column_types: list)."""
    col_names = []
    col_types = []
    for node in _find_all(root, lambda n: n.type == "column_definition"):
        children = node.named_children
        name_node = next((c for c in children if c.type == "identifier"), None)
        if name_node:
            col_names.append(_text(name_node, src).strip())
            col_type = _col_type_text(name_node, children, src)
            if col_type:
                col_types.append(col_type)
    return col_names, col_types


def extract_column_sigs(root, src: bytes) -> list:
    """Extract column signatures as 'TableName.ColumnName TYPE' strings.
    Returns one entry per column defined in a CREATE TABLE statement."""
    sigs = []
    for table_node in _find_all(root, lambda n: n.type == "create_table"):
        ref = next((c for c in table_node.children if c.type == "object_reference"), None)
        table_name = _object_name(ref, src) if ref else ""
        for col_node in _find_all(table_node, lambda n: n.type == "column_definition"):
            children = col_node.named_children
            name_node = next((c for c in children if c.type == "identifier"), None)
            if not name_node:
                continue
            col_name = _text(name_node, src).strip()
            col_type = _col_type_text(name_node, children, src)
            prefix = f"{table_name}." if table_name else ""
            sigs.append(f"{prefix}{col_name} {col_type}".strip())
    return sigs


def extract_referenced_tables(root, src: bytes) -> list:
    """Extract table names referenced in FROM, JOIN, and REFERENCES clauses."""
    names = []
    # FROM clause relations
    for node in _find_all(root, lambda n: n.type == "relation"):
        ref = next((c for c in node.children if c.type == "object_reference"), None)
        if ref:
            name = _full_object_name(ref, src)
            if name:
                names.append(name)
        elif node.type == "relation" and node.named_children:
            # Sometimes the object_reference IS the relation
            for c in node.children:
                if c.type == "object_reference":
                    name = _full_object_name(c, src)
                    if name:
                        names.append(name)

    # Direct object_references in FROM nodes
    for node in _find_all(root, lambda n: n.type == "from"):
        for ref in _find_all(node, lambda n: n.type == "object_reference"):
            name = _full_object_name(ref, src)
            if name:
                names.append(name)

    # REFERENCES in foreign keys
    for node in _find_all(root, lambda n: n.type == "constraint"):
        refs = _find_all(node, lambda n: n.type == "object_reference")
        for ref in refs:
            name = _full_object_name(ref, src)
            if name:
                names.append(name)

    return names


def extract_invocations(root, src: bytes) -> list:
    """Extract function/procedure call names from invocation nodes."""
    names = []
    for node in _find_all(root, lambda n: n.type == "invocation"):
        # First child is usually the function name
        for child in node.children:
            if child.type == "object_reference":
                name = _object_name(child, src)
                if name:
                    names.append(name)
                break
            elif child.type == "identifier":
                name = _text(child, src).strip()
                if name:
                    names.append(name)
                break
    return names
