"""
inspect_doc.py — show exactly what the indexer would send to Typesense for a file,
and optionally diff against what is currently stored in the index.

Usage:
    python inspect_doc.py <file>              # show extracted doc
    python inspect_doc.py <file> --diff       # also compare with live Typesense doc
    python inspect_doc.py <file> --field symbols   # show one field only
    python inspect_doc.py <file> --field class_names --diff

<file> accepts:
    Windows path:   C:/myproject/src/Services/Widget.cs
    $SRC_ROOT path: $SRC_ROOT/Services/Widget.cs
    WSL path:       /mnt/c/myproject/src/Services/Widget.cs

Examples:
    python inspect_doc.py "$SRC_ROOT/Services/Widget.cs" --diff
    python inspect_doc.py "$SRC_ROOT/Services/Widget.cs" --field class_names
"""

import os
import sys
import re
import json
import argparse
import urllib.request
import urllib.error

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _root)

from indexserver.config import SRC_ROOT, HOST, PORT, API_KEY, COLLECTION, to_native_path
from indexserver.indexer import build_document, file_id


# ---------------------------------------------------------------------------
# Path normalisation (mirrors mcp_server._normalize_files_glob)
# ---------------------------------------------------------------------------

def resolve_path(raw: str) -> str:
    """Convert any supported path format to an absolute native OS path."""
    p = raw.strip()
    p = p.replace("${SRC_ROOT}", SRC_ROOT).replace("$SRC_ROOT", SRC_ROOT)
    # WSL /mnt/<drive>/... → <drive>:/...
    m = re.match(r"^/mnt/([a-z])/(.+)$", p)
    if m:
        p = m.group(1).upper() + ":/" + m.group(2)
    return to_native_path(p)


# ---------------------------------------------------------------------------
# Live Typesense fetch
# ---------------------------------------------------------------------------

def fetch_live_doc(doc_id: str) -> dict | None:
    url = f"http://{HOST}:{PORT}/collections/{COLLECTION}/documents/{doc_id}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

# Fields where content is long — print truncated
_TOKEN_FIELDS = {"tokens"}

# Fields that are lists — show count + first N items
_LIST_FIELDS = {
    "class_names", "method_names", "member_sigs", "base_types",
    "field_types", "local_types", "param_types", "return_types",
    "cast_types", "type_refs", "call_sites", "member_accesses",
    "attr_names", "usings",
}

# Scalar fields shown as-is
_SCALAR_FIELDS = {
    "id", "relative_path", "filename", "extension",
    "subsystem", "language", "namespace", "mtime",
}

_ALL_FIELDS = _SCALAR_FIELDS | _LIST_FIELDS | _TOKEN_FIELDS

_FIELD_ORDER = [
    # identity
    "id", "relative_path", "filename", "extension", "language", "subsystem",
    "namespace", "mtime",
    # declaration fields
    "class_names", "method_names", "member_sigs",
    # type reference fields
    "base_types", "field_types", "local_types",
    "param_types", "return_types", "cast_types", "type_refs",
    # call and access site fields
    "call_sites", "member_accesses",
    # other
    "attr_names", "usings",
    # tokens last (large)
    "tokens",
]

TRUNCATE_LIST  = 20   # show at most this many items per list field
TRUNCATE_CONTENT = 120  # chars of content to show


def _fmt_list(values: list, truncate: int = TRUNCATE_LIST) -> str:
    count = len(values)
    shown = values[:truncate]
    suffix = f"  … +{count - truncate} more" if count > truncate else ""
    items = "\n".join(f"    {v}" for v in shown)
    return f"[{count}]{suffix}\n{items}" if items else f"[{count}]"


def print_doc(doc: dict, label: str = "", field_filter: str = "") -> None:
    if label:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")

    fields = [field_filter] if field_filter else _FIELD_ORDER

    for key in fields:
        if key not in doc:
            if field_filter:
                print(f"  {key}: <not present>")
            continue
        val = doc[key]
        if key in _TOKEN_FIELDS:
            token_list = val.split() if isinstance(val, str) else []
            preview = val[:TRUNCATE_CONTENT].replace("\n", " ")
            ellipsis = "…" if len(val) > TRUNCATE_CONTENT else ""
            print(f"\n  {key}: [{len(token_list)} tokens]")
            print(f"    {preview}{ellipsis}")
        elif key in _LIST_FIELDS:
            print(f"\n  {key}: {_fmt_list(val)}")
        else:
            print(f"  {key}: {val}")


def diff_docs(extracted: dict, live: dict, field_filter: str = "") -> None:
    print(f"\n{'='*60}")
    print("  DIFF: extracted (local) vs live (Typesense)")
    print(f"{'='*60}")

    fields = [field_filter] if field_filter else _FIELD_ORDER

    any_diff = False
    for key in fields:
        if key in _TOKEN_FIELDS:
            # compare token counts only
            e_tokens = set(extracted.get(key, "").split())
            l_tokens = set(live.get(key, "").split())
            added   = sorted(e_tokens - l_tokens)
            removed = sorted(l_tokens - e_tokens)
            if added or removed:
                any_diff = True
                print(f"\n  {key}: token diff (extracted vs live)")
                if added:
                    print(f"    + {len(added)} tokens only in extracted")
                if removed:
                    print(f"    - {len(removed)} tokens only in live")
            continue

        e_val = extracted.get(key)
        l_val = live.get(key)

        if isinstance(e_val, list) and isinstance(l_val, list):
            e_set = set(e_val)
            l_set = set(l_val)
            added   = sorted(e_set - l_set)
            removed = sorted(l_set - e_set)
            if added or removed:
                any_diff = True
                print(f"\n  {key}:")
                for v in added:
                    print(f"    + {v}")
                for v in removed:
                    print(f"    - {v}")
        elif e_val != l_val:
            any_diff = True
            print(f"\n  {key}:")
            print(f"    extracted: {e_val}")
            print(f"    live:      {l_val}")

    if not any_diff:
        print("\n  (no differences)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("file", help="Path to the source file to inspect")
    ap.add_argument("--diff", action="store_true",
                    help="Compare extracted doc with what is currently in Typesense")
    ap.add_argument("--field", metavar="NAME", default="",
                    help="Show only this field (e.g. class_names, member_sigs, cast_types)")
    ap.add_argument("--live-only", action="store_true",
                    help="Show only the live Typesense doc (no local extraction)")
    args = ap.parse_args()

    abs_path = resolve_path(args.file)
    if not os.path.isfile(abs_path):
        print(f"ERROR: file not found: {abs_path}", file=sys.stderr)
        sys.exit(1)

    # Derive relative path the same way the indexer does
    src_root = to_native_path(SRC_ROOT)
    try:
        rel_path = os.path.relpath(abs_path, src_root).replace("\\", "/")
    except ValueError:
        rel_path = os.path.basename(abs_path)

    doc_id = file_id(rel_path)
    print(f"  file:     {abs_path}")
    print(f"  rel_path: {rel_path}")
    print(f"  doc_id:   {doc_id}")

    # --- live doc ---
    live_doc = None
    if args.diff or args.live_only:
        try:
            live_doc = fetch_live_doc(doc_id)
        except Exception as e:
            print(f"  WARNING: could not fetch live doc: {e}", file=sys.stderr)

        if live_doc is None:
            print(f"  WARNING: document not found in Typesense index (id={doc_id})")
        elif args.live_only:
            print_doc(live_doc, label="LIVE (Typesense)", field_filter=args.field)
            return

    if not args.live_only:
        # --- extracted doc ---
        extracted = build_document(abs_path, rel_path)
        if extracted is None:
            print("ERROR: build_document returned None (file unreadable?)", file=sys.stderr)
            sys.exit(1)
        print_doc(extracted, label="EXTRACTED (local)", field_filter=args.field)

        if args.diff and live_doc is not None:
            print_doc(live_doc, label="LIVE (Typesense)", field_filter=args.field)
            diff_docs(extracted, live_doc, field_filter=args.field)
        elif args.diff and live_doc is None:
            print("\n  (skipping diff — live doc not available)")


if __name__ == "__main__":
    main()
