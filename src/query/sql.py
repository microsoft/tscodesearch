"""
SQL query functions for process_sql_file.

Uses regex-based matching rather than tree-sitter AST because the
tree-sitter-sql grammar doesn't support T-SQL constructs (CREATE
PROCEDURE, etc.).  Regex is reliable for SQL's keyword-heavy syntax.
"""

import re



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
