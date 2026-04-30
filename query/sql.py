"""
SQL query functions for process_sql_file.

Uses regex-based matching rather than tree-sitter AST because the
tree-sitter-sql grammar doesn't support T-SQL constructs (CREATE
PROCEDURE, etc.).  Regex is reliable for SQL's keyword-heavy syntax.

Also provides AST-based extraction helpers (extract_table_names, etc.)
used by the indexer for semantic field population.
"""

import re
import sys
from ._util import _dedupe, _make_matches, FileDescription, ClassInfo, MethodInfo, FieldInfo, CallSiteInfo

try:
    import tree_sitter_sql as tssql
    from tree_sitter import Language, Parser as _Parser
    _SQL_LANG   = Language(tssql.language())
    _sql_parser = _Parser(_SQL_LANG)
    _SQL_AVAILABLE = True
except Exception:
    _sql_parser    = None
    _SQL_AVAILABLE = False



# ── Regex patterns ─────────────────────────────────────────────────────────────

_CREATE_TABLE_RE = re.compile(
    r'(?i)^\s*CREATE\s+TABLE\s+(\[?[\w.]+\]?\.)?(\[?[\w]+\]?)',
    re.MULTILINE,
)
_CREATE_VIEW_RE = re.compile(
    r'(?i)^\s*CREATE\s+VIEW\s+(\[?[\w.]+\]?\.)?(\[?[\w]+\]?)',
    re.MULTILINE,
)
_CREATE_PROC_RE = re.compile(
    r'(?i)^\s*CREATE\s+(?:OR\s+ALTER\s+)?PROC(?:EDURE)?\s+(\[?[\w.]+\]?\.)?(\[?[\w]+\]?)',
    re.MULTILINE,
)
_CREATE_FUNC_RE = re.compile(
    r'(?i)^\s*CREATE\s+(?:OR\s+ALTER\s+)?FUNCTION\s+(\[?[\w.]+\]?\.)?(\[?[\w]+\]?)',
    re.MULTILINE,
)
_ALTER_TABLE_RE = re.compile(
    r'(?i)^\s*ALTER\s+TABLE\s+(\[?[\w.]+\]?\.)?(\[?[\w]+\]?)',
    re.MULTILINE,
)

# Column definitions: indented identifier followed by a type keyword
_COLUMN_DEF_RE = re.compile(
    r'(?i)^\s+(\[?(?!(?:CONSTRAINT|PRIMARY|FOREIGN|UNIQUE|CHECK|INDEX|DEFAULT|NOT|NULL)\b)'
    r'[\w]+\]?)\s+'
    r'(INT|BIGINT|SMALLINT|TINYINT|BIT|NVARCHAR|VARCHAR|CHAR|NCHAR|'
    r'DATETIME|DATETIME2|DATE|TIME|DATETIMEOFFSET|DECIMAL|NUMERIC|FLOAT|REAL|MONEY|'
    r'UNIQUEIDENTIFIER|VARBINARY|BINARY|IMAGE|TEXT|NTEXT|XML|SQL_VARIANT)',
    re.MULTILINE,
)


def _match_name(m):
    """Extract the unqualified name from a regex match with optional schema prefix."""
    schema = (m.group(1) or "").strip().rstrip(".").strip("[]")
    name = m.group(2).strip("[]")
    return f"{schema}.{name}" if schema else name


def _line_of(text, pos):
    """Return 1-based line number for a character position."""
    return text.count("\n", 0, pos) + 1


def _find_enclosing_table(text, pos):
    """Find the nearest CREATE TABLE name before a position."""
    best = None
    for m in _CREATE_TABLE_RE.finditer(text):
        if m.start() > pos:
            break
        best = _match_name(m)
    return best or ""


# ── Query functions ───────────────────────────────────────────────────────────
# All return list[(line_num_str, text)]

def sql_q_text(lines, pattern):
    """Plain text search — find lines containing pattern (case-insensitive)."""
    results = []
    pat = pattern.lower()
    for i, line in enumerate(lines, 1):
        if pat in line.lower():
            results.append((str(i), line))
    return results


def sql_q_declarations(text, lines, pattern):
    """Find CREATE TABLE/VIEW/PROC/FUNCTION declarations matching pattern."""
    results = []
    pat = pattern.lower() if pattern else None
    for regex in [_CREATE_TABLE_RE, _CREATE_VIEW_RE, _CREATE_PROC_RE,
                  _CREATE_FUNC_RE, _ALTER_TABLE_RE]:
        for m in regex.finditer(text):
            name = _match_name(m)
            if pat and pat not in name.lower():
                continue
            line_num = _line_of(text, m.start())
            results.append((str(line_num), lines[line_num - 1] if line_num <= len(lines) else m.group(0)))
    results.sort(key=lambda x: int(x[0]))
    return results


def sql_q_fields(text, lines, pattern):
    """Find column definitions matching pattern.
    Leverages member_sigs format: TableName.ColumnName TYPE."""
    results = []
    pat = pattern.lower() if pattern else None
    for m in _COLUMN_DEF_RE.finditer(text):
        col_name = m.group(1).strip("[]")
        col_type = m.group(2).upper()
        table = _find_enclosing_table(text, m.start())
        sig = f"{table}.{col_name} {col_type}" if table else f"{col_name} {col_type}"
        if pat and pat not in col_name.lower() and pat not in sig.lower():
            continue
        line_num = _line_of(text, m.start())
        results.append((str(line_num), lines[line_num - 1] if line_num <= len(lines) else sig))
    return results


def sql_q_calls(text, lines, pattern):
    """Find references to a table/proc/function name in FROM, JOIN, EXEC, REFERENCES."""
    results = []
    if not pattern:
        return results
    # Match the pattern as a word in relevant SQL contexts
    ref_re = re.compile(
        r'(?i)\b(?:FROM|JOIN|EXEC|EXECUTE|REFERENCES|INTO|UPDATE|INSERT\s+INTO)\s+'
        r'(?:\[?[\w.]+\]?\.)?(\[?' + re.escape(pattern) + r'\]?)\b',
        re.MULTILINE,
    )
    for m in ref_re.finditer(text):
        line_num = _line_of(text, m.start())
        results.append((str(line_num), lines[line_num - 1] if line_num <= len(lines) else m.group(0)))
    seen = set()
    deduped = []
    for ln, txt in results:
        if ln not in seen:
            seen.add(ln)
            deduped.append((ln, txt))
    return deduped


def sql_q_classes(text, lines):
    """List all table and view names (CREATE TABLE / CREATE VIEW)."""
    results = []
    for regex in [_CREATE_TABLE_RE, _CREATE_VIEW_RE]:
        for m in regex.finditer(text):
            name = _match_name(m)
            line_num = _line_of(text, m.start())
            results.append((str(line_num), f"TABLE/VIEW {name}"))
    results.sort(key=lambda x: int(x[0]))
    return results


def sql_q_methods(text, lines):
    """List all stored procedure and function names."""
    results = []
    for regex in [_CREATE_PROC_RE, _CREATE_FUNC_RE]:
        for m in regex.finditer(text):
            name = _match_name(m)
            line_num = _line_of(text, m.start())
            results.append((str(line_num), f"PROC/FUNC {name}"))
    results.sort(key=lambda x: int(x[0]))
    return results


# ── AST-based extraction helpers (used by indexer for semantic fields) ────────
# These operate on tree-sitter parse trees — no parser state here.

def _find_all(node, predicate, results=None):
    if results is None:
        results = []
    stack = [node]
    while stack:
        n = stack.pop()
        if predicate(n):
            results.append(n)
        stack.extend(reversed(n.children))
    return results


def _ast_text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


_SQL_LITERAL_NODES = {"comment", "marginalia", "string"}

_AST_TYPE_DECL_NODES = {"create_table", "create_view", "alter_table"}

_FUNCTION_DECL_NODES = {"create_function"}

_PROC_DECL_NODES = {"create_procedure"}


def _ast_line(node) -> int:
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
            return _ast_text(idents[-1], src).strip()
    if node.type == "identifier":
        return _ast_text(node, src).strip()
    return _ast_text(node, src).strip()


def _schema_name(node, src: bytes) -> str:
    """Extract schema prefix from object_reference ('dbo.Users' → 'dbo')."""
    if node is None or node.type != "object_reference":
        return ""
    idents = [c for c in node.children if c.type == "identifier"]
    if len(idents) >= 2:
        return _ast_text(idents[0], src).strip()
    return ""


def _full_object_name(node, src: bytes) -> str:
    """Return schema.name if schema exists, else just name."""
    schema = _schema_name(node, src)
    name = _object_name(node, src)
    if schema and name:
        return f"{schema}.{name}"
    return name


def extract_table_names(root, src: bytes) -> list:
    """Find all table/view names from CREATE TABLE / CREATE VIEW / ALTER TABLE."""
    names = []
    for node in _find_all(root, lambda n: n.type in _AST_TYPE_DECL_NODES):
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
            args_text = _ast_text(args, src).strip() if args else "()"
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
            return _ast_text(child, src).strip().upper()
        # UNIQUEIDENTIFIER and other user-defined types land as object_reference
        if ctype == "object_reference":
            return _ast_text(child, src).strip().upper()
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
            col_names.append(_ast_text(name_node, src).strip())
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
            col_name = _ast_text(name_node, src).strip()
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
                name = _ast_text(child, src).strip()
                if name:
                    names.append(name)
                break
    return names




# ── Process function ──────────────────────────────────────────────────────────

def query_sql_bytes(src_bytes: bytes, mode: str, mode_arg: str, **kwargs):
    """Parse SQL bytes and return list[{"line": N, "text": "..."}] for the given mode."""
    text = src_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()

    dispatch = {
        "text":         lambda: sql_q_text(lines, mode_arg),
        "declarations": lambda: sql_q_declarations(text, lines, mode_arg),
        "fields":       lambda: sql_q_fields(text, lines, mode_arg),
        "calls":        lambda: sql_q_calls(text, lines, mode_arg),
        "classes":      lambda: sql_q_classes(text, lines),
        "methods":      lambda: sql_q_methods(text, lines),
    }

    fn = dispatch.get(mode) or (lambda: sql_q_text(lines, mode_arg) if mode_arg else [])
    return _make_matches(fn() or [])


def describe_sql_file(src_bytes: bytes, ext: str = "") -> FileDescription:
    """Return all structured SQL data from src_bytes as a FileDescription."""
    classes: list = []   # tables / views → ClassInfo
    methods: list = []   # procs / functions → MethodInfo
    fields:  list = []   # columns → FieldInfo
    calls:   list = []   # referenced tables + invocations → CallSiteInfo

    if _SQL_AVAILABLE and _sql_parser is not None:
        try:
            tree = _sql_parser.parse(src_bytes)
            root = tree.root_node
            for name in extract_table_names(root, src_bytes):
                classes.append(ClassInfo(line=0, name=name, kind="table"))
            for name in extract_function_names(root, src_bytes):
                methods.append(MethodInfo(line=0, name=name, kind="function"))
            for name in extract_proc_names_ast(root, src_bytes):
                methods.append(MethodInfo(line=0, name=name, kind="procedure"))
            for sig in extract_proc_sigs(root, src_bytes):
                methods.append(MethodInfo(line=0, name="", kind="procedure", sig=sig))
            for _proc, table in extract_proc_body_refs(root, src_bytes):
                calls.append(CallSiteInfo(name=table))
            for col_sig in extract_column_sigs(root, src_bytes):
                parts = col_sig.split(" ", 1)
                col_type = parts[1] if len(parts) > 1 else ""
                col_name = parts[0].split(".")[-1]
                fields.append(FieldInfo(line=0, name=col_name, kind="column",
                                        field_type=col_type, sig=col_sig))
            for ref in extract_referenced_tables(root, src_bytes):
                calls.append(CallSiteInfo(name=ref))
            for inv in extract_invocations(root, src_bytes):
                calls.append(CallSiteInfo(name=inv))
        except Exception:
            pass

    for name in extract_proc_names_regex(src_bytes):
        methods.append(MethodInfo(line=0, name=name, kind="procedure"))

    return FileDescription(
        language="sql",
        classes=classes,
        methods=methods,
        fields=fields,
        call_site_infos=calls,
    )
