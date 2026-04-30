def _make_matches(results):
    """Convert (line_str, text) tuples from query functions to list[{"line": N, "text": "..."}]."""
    out = []
    for line_num_str, text in results:
        try:
            line_int = int(line_num_str)
        except (ValueError, TypeError):
            line_int = 0
        out.append({"line": line_int, "text": (text or "").rstrip()})
    return out
