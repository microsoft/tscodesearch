"""
Shared search-parameter resolvers.

Translates a high-level search intent (mode + uses_kind + symbol_kind, plus
ext / sub / exclude_path filters) into the low-level ``query_by`` / ``weights``
/ ``filter_by`` strings consumed by ``indexserver.search.search()``.

Two call sites use this:

  * ``tsquery_server`` for the daemon's ``POST /query-codebase`` endpoint.
  * ``scripts/search.py`` for the standalone read-only CLI.

Keeping the mapping here means new modes (or new ``uses_kind`` values) appear
in both places automatically.
"""

from __future__ import annotations

from indexserver.config import normalize_path


# -- Mode -> (query_by, weights) ------------------------------------------------

def resolve_query_params(ts_mode_flag: str, uses_kind: str = "", symbol_kind: str = ""
                        ) -> tuple[str, str]:
    """Return ``(query_by, weights)`` for the given mode flag.

    ``ts_mode_flag`` is one of: ``implements``, ``calls``, ``uses``, ``attrs``,
    ``casts``, ``accesses_of``, ``symbols``, ``all_refs``. Unknown flags fall
    back to the broad ``all_refs`` mapping so callers can't crash the search.
    """
    if ts_mode_flag == "implements":
        return "base_types,class_names,path_tokens", "4,3,2"
    if ts_mode_flag == "calls":
        # ``qualified_calls`` carries ``Type.Method`` tokens (static-style
        # ``Foo.Bar`` plus method-scoped resolved-receiver forms like
        # ``IRepository.Save`` when the indexer pinned the type). Querying
        # both fields lets the agent pass either a bare ``Save`` or a
        # qualified ``IRepository.Save`` without picking the right field
        # themselves.
        return "call_sites,qualified_calls,path_tokens", "4,4,2"
    if ts_mode_flag == "uses":
        k = (uses_kind or "all").lower().strip()
        if k == "field":   return "field_types,path_tokens", "4,2"
        if k == "param":   return "param_types,path_tokens", "4,2"
        if k == "return":  return "return_types,path_tokens", "4,2"
        if k == "cast":    return "cast_types,path_tokens", "4,2"
        if k == "base":    return "base_types,class_names,path_tokens", "4,3,2"
        if k == "locals":  return "local_types,path_tokens", "4,2"
        return "type_refs,cast_types,path_tokens", "4,3,2"
    if ts_mode_flag == "attrs":       return "attr_names,path_tokens", "4,2"
    if ts_mode_flag == "casts":       return "cast_types,path_tokens", "4,2"
    if ts_mode_flag == "accesses_of": return "member_accesses,path_tokens", "4,2"
    if ts_mode_flag == "symbols":
        # symbol_kind_query_by lives in query.cs to keep language-specific
        # symbol-kind knowledge there; import lazily so this module stays
        # cheap to load.
        from query.cs import symbol_kind_query_by
        narrowed = symbol_kind_query_by(symbol_kind or "")
        return (narrowed or "class_names,method_names,path_tokens"), "4,3,2"
    # all_refs and the fallback share the same mapping.
    return "path_tokens,class_names,method_names,tokens", "5,4,4,1"


# -- ext / sub / exclude_path -> filter_by --------------------------------------

_CPP_SRC = frozenset({"cpp", "cc", "cxx", "c"})
_CPP_HDR = frozenset({"h", "hpp", "hxx"})


def build_filter_by(ext: str, sub: str, exclude_path: str) -> str:
    """Build the ``filter_by`` string for a search.

    Behavior matches what callers expect:

      * ``ext`` accepts comma-separated extensions. When any C/C++ source
        extension is requested, C/C++ headers (``.h``/``.hpp``/``.hxx``) are
        automatically included so ``implements``/``uses`` finds class
        declarations that live in headers.
      * ``sub`` accepts comma-separated folder paths. Multiple values OR.
      * ``exclude_path`` accepts comma-separated folder paths to exclude.
    """
    parts: list[str] = []

    if ext:
        exts = {e.lstrip(".") for e in ext.split(",") if e.strip()}
        if exts & _CPP_SRC:
            exts |= _CPP_HDR
        if len(exts) == 1:
            parts.append(f"extension:={next(iter(exts))}")
        elif exts:
            parts.append(f"extension:=[{','.join(sorted(exts))}]")

    if sub:
        included = [normalize_path(p).strip("/") for p in sub.split(",")]
        included = [p for p in included if p]
        if len(included) == 1:
            parts.append(f"path_segments:={included[0]}")
        elif included:
            parts.append(f"path_segments:=[{','.join(included)}]")

    if exclude_path:
        excluded = [normalize_path(p).strip("/") for p in exclude_path.split(",")]
        excluded = [p for p in excluded if p]
        if len(excluded) == 1:
            parts.append(f"path_segments:!={excluded[0]}")
        elif excluded:
            parts.append(f"path_segments:!=[{','.join(excluded)}]")

    return " && ".join(parts)
