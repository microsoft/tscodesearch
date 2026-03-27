# Guided Testing Loop

A repeatable process for finding and fixing bugs in tscodesearch by comparing
live AST query output against grep ground-truth.

---

## Overview

The core idea is simple: for any query mode and pattern, grep is the oracle.
Every line grep finds that the tool misses is a potential bug; every line the
tool returns that grep doesn't is a potential false positive. Work through real
files, compare outputs, trace discrepancies to the AST walker, fix, and pin
with tests.

---

## Prerequisites

- Service running and index populated (`mcp__tscodesearch__ready`)
- Python environment with tree-sitter and the indexserver installed
- Test command: `MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/ -v"`

---

## The Loop

```
1. Health check       — confirm service is up, index is populated
2. Pick a file        — choose a real source file from the indexed codebase;
                        prefer medium-sized files with varied syntax
3. Pick a mode        — one of: calls, accesses_on, accesses_of, uses,
                        uses_kind=*, declarations, implements, methods, etc.
4. Pick a pattern     — a type name, method name, or member name that appears
                        multiple times in the file in different syntactic contexts
5. Run the tool       — query_single_file(mode, pattern, file=…)
6. Run grep           — Grep(pattern, path=file) for ground-truth
7. Compare            — does every grep hit appear in the tool output?
                        does the tool return anything grep would not find?
8. Classify           — real miss / real false positive / expected difference
9. Root-cause         — read src/query/cs.py; find the function; check which
                        AST node types its walker covers
10. Write fixture     — minimal synthetic .cs file in sample/root1/
11. Write tests       — xfail tests that pin the gap, plus regression guards
12. Fix               — patch the walker; promote tests to passing
13. Full suite        — run all tests; fix any doc-count assertions if you
                        added a fixture file
```

---

## Comparing Tool Output vs Grep

Run both, then go line by line:

- **Miss** (grep finds, tool doesn't): likely a missing node type in the walker.
  Check whether the missing line uses a syntactic form not yet covered
  (property vs field, `?.` vs `.`, pattern binding vs plain local, etc.).

- **False positive** (tool finds, grep doesn't): less common; check whether
  the tool is matching inside string literals, comments, or unrelated identifiers.
  Some differences are intentional (e.g. the tool deduplicates same-line hits,
  includes semantic context that raw grep can't see).

- **Expected difference**: the tool returns richer output (variable names,
  enclosing class context) — a result that looks "extra" may just be formatted
  differently. Verify by checking the line number, not the text.

**Verify before declaring a bug.** Grep matches substrings: `typeof(Connection)`
matches a search for `Connection`, but may not be the semantic hit you think.
Read the actual source line before concluding anything is missing or spurious.

---

## Inspecting the AST

When you need to understand why a node is or isn't being found, write a small
Python script to a temp file and run it via WSL:

```python
# /tmp/inspect.py
import sys
sys.path.insert(0, "/mnt/<drive>/<path>/tscodesearch")
from src.query.cs import _parse_src   # or equivalent
import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

LANG = Language(tscsharp.language())
parser = Parser(LANG)

src = open("/mnt/<drive>/<path>/file.cs", encoding="utf-8").read()
tree = parser.parse(src.encode())

def walk(node, indent=0):
    field_info = ""
    # print node type, field name if known, and source text snippet
    print(" " * indent + node.type, repr(node.text[:60] if node.text else b""))
    for child in node.children:
        walk(child, indent + 2)

walk(tree.root_node)
```

Run with:
```
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "~/.local/indexserver-venv/bin/python3 /tmp/inspect.py"
```

Key things to check:
- What is the **node type** of the construct you're interested in?
- What are the **field names** (`child_by_field_name`) for type and variable name?
- Is the node a direct child of something, or nested inside an intermediate node?

---

## Common Bug Pattern

Almost every bug found in guided testing is the same root cause:

> The AST walker enumerates a fixed set of node types. A new syntactic form
> uses a *different* node type that was never added to the set. The walker
> silently skips it.

Known examples of this pattern, across several guided testing rounds:

| Syntactic form | Node type missed | Affected mode |
|---|---|---|
| `public T Prop { get; }` | `property_declaration` | accesses_on |
| `foreach (T v in ...)` | `foreach_statement` | accesses_on |
| `if (x is T v)` / `case T v:` | `declaration_pattern` | accesses_on, uses_kind=locals |
| `method(out T v)` | `declaration_expression` | accesses_on, uses_kind=locals |
| `obj?.Method()` | `conditional_access_expression` | calls, accesses_on, accesses_of |
| `obj?.Member` | `member_binding_expression` | accesses_on, accesses_of |
| `using (T v = ...)` | `using_statement` | uses_kind=locals |
| `for (T v = ...; ...)` | `for_statement` | uses_kind=locals |
| `method_declaration` return type | wrong field name (`type` vs `returns`) | uses_kind=return |
| `delegate_declaration` | omitted from node-type set | uses_kind=return |
| `out`/`ref` modifier | wrong node type (`modifier` vs `parameter_modifier`) | uses_kind=param |
| `x as T` | `as_expression` (`right` field) — only `cast_expression` was handled | uses_kind=cast |
| `new T { Prop = v }` | `initializer_expression` → `assignment_expression` inside `object_creation_expression` | accesses_on, accesses_of |
| `x with { Prop = v }` | `with_initializer` inside `with_expression` | accesses_on, accesses_of |
| `if (x is T { Prop: v } name)` | `recursive_pattern` (`type`/`name` fields same as `declaration_pattern`) | accesses_on, uses_kind=locals |

When you find a new miss, look for the pattern: same logical intent, different
node type.

---

## Writing Fixtures

A good fixture file:

- Lives in `sample/root1/` with a descriptive name (e.g. `ForeachAccess.cs`)
- Uses **generic types only** — no SPO-specific imports or dependencies
- Contains the **positive case** (the syntax that was missed)
- Contains a **regression guard** (a plain form that already worked)
- Contains a **negative case** (a different type that must NOT appear in results)
- Has short, readable methods; each method tests one syntactic form

Example structure:
```csharp
namespace Sample
{
    public class Widget { public void Use() { } }
    public class Other  { public void Use() { } }

    public class Service
    {
        // regression guard — plain local must still be found
        public void PlainLocal() {
            Widget w = new Widget(); w.Use();
        }

        // the new form being tested
        public void NewSyntaxForm() {
            // ... syntax under test ...
        }

        // negative — Other must NOT appear in Widget results
        public void NegativeCase() {
            Other o = new Other(); o.Use();
        }
    }
}
```

After adding a fixture file, update the doc-count assertion in
`test_sample_e2e.py` (`test_collection_has_ten_files` and
`test_root1_doc_count_equals_nine`) to reflect the new total.

---

## Writing Tests

Test file conventions:

- One file per bug: `tests/test_cs_<topic>.py`
- Module-level parse at import time (fast, no repeated I/O):
  ```python
  with open(_SAMPLE, encoding="utf-8") as _f:
      _SRC = _f.read()
  _PARSED = _parse(_SRC)   # pass str, not bytes
  _LINES  = _SRC.splitlines()
  ```
- Helper to find a line number by fragment:
  ```python
  def _line_no(fragment):
      for i, ln in enumerate(_LINES):
          if fragment in ln:
              return i + 1
      raise AssertionError(f"Fragment not found: {fragment!r}")
  ```
  Use a **specific enough fragment** that it won't accidentally match a comment
  containing the same text.
- Three test categories per bug:
  1. **The miss** — assert the new syntax form is found (was the bug)
  2. **Regression guard** — assert the old syntax form still works
  3. **Negative** — assert the wrong type does NOT appear

Workflow:
1. Write tests with `@unittest.expectedFailure` to pin the gap
2. Fix the implementation
3. Remove `expectedFailure`; confirm all pass

---

## Running Tests

Single file:
```
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/test_cs_<topic>.py -v"
```

Full suite:
```
MSYS_NO_PATHCONV=1 wsl.exe bash -lc "cd /mnt/<drive>/<path>/tscodesearch && ~/.local/indexserver-venv/bin/pytest tests/ -v"
```

The e2e tests (`test_sample_e2e.py`) require Typesense running; they are
skipped automatically when the service is not available.

---

## Documenting the Round

After each fix, record it somewhere persistent (a changelog file, a wiki page,
or inline in a `guidedtesting.md` alongside the test suite). Include:

```markdown
## Round N: <short description>

### What triggered it
<file/query/grep comparison that revealed the gap>

### Root cause
<which node type was missing and why; AST snippet if helpful>

### Fix
<the code change, with a before/after snippet>

### Test artifacts
| File | Purpose |
|------|---------|
| `sample/root1/Fixture.cs` | ... |
| `tests/test_cs_topic.py`  | ... |
```

---

## Choosing What to Test Next

Good candidates for future rounds:

- **Modes not yet stress-tested** against real files: `casts`, `ident`, `usings`
- **Unusual C# syntax**: `await using`, record types, primary constructors,
  `switch` expressions (not just switch statements), `with` expressions,
  tuple deconstruction (`var (a, b) = ...`)
- **Chained access**: `a?.b?.c` produces nested `conditional_access_expression`
  nodes; deep chains may expose missed inner bindings
- **Generic constraints**: `where T : IFoo` — does `implements` handle these?
- **Multi-declarator locals**: `int x = 1, y = 2;` — does `uses_kind=locals`
  find both `x` and `y`?
- **Lambda parameters**: `list.Select((Widget w) => w.Use())` — `parameter`
  inside a lambda has the same node type as a method parameter; confirm it
  is or isn't tracked by design
