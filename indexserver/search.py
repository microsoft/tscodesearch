"""
Code-index search built on top of the Tantivy backend.

The exposed ``search()`` returns:

    {
      "found":        int,
      "hits":         [{"document": {...}, "highlights": [...]}],
      "facet_counts": [{"field_name": "path_segments", "counts": [...]}],
    }

Inputs map onto Tantivy primitives like so:

  * query_by="filename,class_names,..."   → multi-field disjunction
    with per-field weights (parsed from a parallel `weights` arg)
  * num_typos=1                           → fuzzy_fields levenshtein distance 1
  * filter_by="extension:=cs && ..."      → BooleanQuery MUST/MUST_NOT terms
  * facet_by="path_segments,..."          → terms aggregation per facet field

Tantivy's query parser handles per-field weighting and per-field fuzziness
natively (see Index.parse_query field_boosts/fuzzy_fields).
"""

from __future__ import annotations

import re
from collections import Counter

import tantivy

from indexserver.backend import Backend


_DEFAULT_FACET_FIELDS = ("path_segments", "language", "extension")
_MAX_FACET_VALUES_DEFAULT = 200


def search(
    backend: Backend,
    *,
    q: str,
    query_by: str,
    weights: str = "",
    per_page: int = 10,
    num_typos: int = 0,
    filter_by: str = "",
    facet_by: str = "",
    max_facet_values: int = _MAX_FACET_VALUES_DEFAULT,
    highlight_fields: str = "",
) -> dict:
    """Run a search and return a result dict."""
    fields  = _split_csv(query_by) or ["filename", "tokens"]
    weight_list = _split_csv(weights)
    field_boosts: dict[str, float] = {}
    for i, f in enumerate(fields):
        if i < len(weight_list):
            try:
                field_boosts[f] = float(weight_list[i])
            except ValueError:
                # Non-numeric weight tokens are ignored — caller stays in the
                # default (unweighted) regime for that field.
                pass
    fuzzy_fields = (
        {f: (False, max(0, int(num_typos)), True) for f in fields}
        if num_typos > 0 else {}
    )

    text_query = _build_text_query(backend, q, fields, field_boosts, fuzzy_fields)
    filters    = _parse_filter_by(backend.schema, filter_by)
    final      = _combine(text_query, filters)

    searcher = backend.searcher()
    results  = searcher.search(final, limit=max(1, per_page))
    found    = results.count if results.count is not None else len(results.hits)

    hits = []
    for score, addr in results.hits:
        doc = searcher.doc(addr).to_dict()
        hits.append({
            "document":   _flatten_doc(doc),
            "highlights": [],
            "score":      score,
        })

    facet_counts = _compute_facets(
        backend, final, _split_csv(facet_by) or list(_DEFAULT_FACET_FIELDS),
        max_facet_values,
    )

    return {
        "found":         found,
        "hits":          hits,
        "facet_counts":  facet_counts,
    }


# ---------------------------------------------------------------------------
# Text query construction
# ---------------------------------------------------------------------------

def _build_text_query(
    backend: Backend, q: str, fields: list[str],
    field_boosts: dict[str, float], fuzzy_fields: dict,
) -> tantivy.Query:
    if not q.strip():
        return tantivy.Query.all_query()

    # Index.parse_query handles multi-field, weighting, and per-field fuzz in
    # one go. The escape step below removes characters that would otherwise be
    # interpreted as Tantivy query syntax (`:`, `+`, `-`, parentheses, etc.).
    # Only pass field_boosts/fuzzy_fields when non-empty — Tantivy rejects None.
    safe_q = _escape_query_text(q)
    kwargs = {"default_field_names": fields}
    if field_boosts:
        kwargs["field_boosts"] = field_boosts
    if fuzzy_fields:
        kwargs["fuzzy_fields"] = fuzzy_fields
    try:
        return backend._index.parse_query(safe_q, **kwargs)  # noqa: SLF001
    except Exception:
        # Fall back to a manual term-OR over all tokens × all fields.
        terms = _tokenize(q)
        if not terms:
            return tantivy.Query.all_query()
        subs = []
        for term in terms:
            for field in fields:
                tq = tantivy.Query.term_query(backend.schema, field, term)
                if field in field_boosts:
                    tq = tantivy.Query.boost_query(tq, field_boosts[field])
                subs.append((tantivy.Occur.Should, tq))
        return tantivy.Query.boolean_query(subs)


_SYNTAX_RE = re.compile(r'([+\-:!~^*?(){}\[\]"\\/])')

def _escape_query_text(q: str) -> str:
    return _SYNTAX_RE.sub(r"\\\1", q)


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

def _tokenize(q: str) -> list[str]:
    return [t.lower() for t in _IDENT_RE.findall(q)]


# ---------------------------------------------------------------------------
# Filter expression parser
# ---------------------------------------------------------------------------
#
# Subset supported:
#     field:=value
#     field:!=value
#     field:=[a,b,c]
#     field:!=[a,b,c]
#     <expr> && <expr>
#
# Values are treated as literal strings against `raw`-tokenized fields.

# Strict format: ``field:=value`` or ``field:!=value``. Parsed by hand instead
# of with a regex so CodeQL's polynomial-redos rule has nothing to flag on the
# externally-supplied filter_by string.
def _parse_clause(clause: str) -> tuple[str, bool, str] | None:
    colon = clause.find(":")
    if colon <= 0:
        return None
    field = clause[:colon]
    if not field.isidentifier():
        return None
    rest = clause[colon + 1:]
    if rest.startswith("!="):
        negate, value = True, rest[2:]
    elif rest.startswith("="):
        negate, value = False, rest[1:]
    else:
        return None
    if not value:
        return None
    return field, negate, value


def _parse_filter_by(schema: tantivy.Schema, expr: str) -> list[tuple]:
    """Parse a filter_by string into a list of (Occur, Query) tuples."""
    expr = (expr or "").strip()
    if not expr:
        return []

    out: list[tuple] = []
    for clause in expr.split("&&"):
        clause = clause.strip()
        if not clause:
            continue
        parsed = _parse_clause(clause)
        if parsed is None:
            continue
        field, negate, raw_v = parsed
        values = _parse_values(raw_v)
        if not values:
            continue
        if len(values) == 1:
            sub = tantivy.Query.term_query(schema, field, values[0])
        else:
            sub = tantivy.Query.term_set_query(schema, field, values)
        out.append(
            (tantivy.Occur.MustNot if negate else tantivy.Occur.Must, sub),
        )
    return out


def _parse_values(raw: str) -> list[str]:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [v.strip() for v in raw.split(",") if v.strip()]


# ---------------------------------------------------------------------------
# Combine, faceting, doc shaping
# ---------------------------------------------------------------------------

def _combine(text_q: tantivy.Query, filters: list[tuple]) -> tantivy.Query:
    if not filters:
        return text_q
    return tantivy.Query.boolean_query(
        [(tantivy.Occur.Must, text_q), *filters],
    )


def _compute_facets(
    backend: Backend, query: tantivy.Query, fields: list[str], max_values: int,
) -> list[dict]:
    """Compute term frequencies per facet field by scanning matching docs.

    Tantivy has a native aggregations API but its dict spec isn't stable across
    tantivy-py versions — for the modest result sets the daemon's overflow path
    deals with (≤1k matches before drill-down) a Python-side count is plenty
    fast and dramatically simpler.
    """
    if not fields:
        return []
    searcher = backend.searcher()
    n = searcher.num_docs
    if n == 0:
        return [{"field_name": f, "counts": []} for f in fields]
    cap = min(n, 5000)
    results = searcher.search(query, limit=cap)
    counters: dict[str, Counter] = {f: Counter() for f in fields}
    for _score, addr in results.hits:
        doc = searcher.doc(addr).to_dict()
        for f in fields:
            for v in doc.get(f) or []:
                counters[f][v] += 1
    return [
        {
            "field_name": f,
            "counts": [
                {"value": v, "count": c}
                for v, c in counters[f].most_common(max_values)
            ],
        }
        for f in fields
    ]


def _flatten_doc(doc: dict) -> dict:
    """Tantivy returns every stored field as a list — unwrap singleton lists.

    Multi-value fields (class_names, …) keep their list shape.
    """
    from indexserver.backend import MULTI_VALUE_FIELDS
    flat: dict = {}
    for name, values in doc.items():
        if not isinstance(values, list):
            flat[name] = values
            continue
        if name in MULTI_VALUE_FIELDS:
            flat[name] = list(values)
        elif len(values) == 1:
            flat[name] = values[0]
        else:
            flat[name] = list(values)
    return flat


def _split_csv(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]
