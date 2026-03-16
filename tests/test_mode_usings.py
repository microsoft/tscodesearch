"""
Tests for usings mode.

mode: --usings (q_usings)
Typesense field: usings (used by --attr filter in mcp_server, also for uses search)

Gaps tested:
  - using directives are returned.
  - using-alias directives are returned.
  - Files with no usings return empty.
  - The namespace declaration itself is NOT a using.
  - Output includes the full directive text.
  - Usings inside namespace blocks are also captured.
"""
from __future__ import annotations

import unittest

from tests.base import _parse
from tests.fixtures import (
    USING_SYSTEM_GENERIC, USING_WITH_ALIAS, NO_USINGS,
)
from indexserver.indexer import extract_cs_metadata
from query import q_usings


# ══════════════════════════════════════════════════════════════════════════════
# q_usings AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQUsings(unittest.TestCase):

    def _usings(self, src):
        return q_usings(*_parse(src))

    def test_finds_using_directives(self):
        r = self._usings(USING_SYSTEM_GENERIC)
        texts = [t for _, t in r]
        assert any("System" in t for t in texts)

    def test_finds_multiple_usings(self):
        r = self._usings(USING_SYSTEM_GENERIC)
        assert len(r) >= 3, \
            f"Expected at least 3 usings (System, Collections.Generic, Tasks): {r}"

    def test_finds_alias_using(self):
        r = self._usings(USING_WITH_ALIAS)
        texts = [t for _, t in r]
        assert any("BS" in t and "Storage" in t for t in texts), \
            f"Alias using must be in output: {texts}"

    def test_no_usings_returns_empty(self):
        r = self._usings(NO_USINGS)
        assert r == []

    def test_namespace_declaration_not_returned(self):
        r = self._usings(USING_SYSTEM_GENERIC)
        texts = [t for _, t in r]
        # 'namespace Synth' must not appear as a using
        for t in texts:
            assert "namespace" not in t, \
                f"Namespace declaration leaked into q_usings: {t!r}"

    def test_output_has_using_keyword(self):
        r = self._usings(USING_SYSTEM_GENERIC)
        texts = [t for _, t in r]
        for t in texts:
            assert t.strip().startswith("using"), \
                f"Output must start with 'using': {t!r}"

    def test_using_inside_namespace_found(self):
        src = """\
namespace Synth {
    using System;
    using System.Collections;

    public class Worker { }
}
"""
        r = self._usings(src)
        texts = [t for _, t in r]
        assert any("System" in t for t in texts)

    def test_global_using_found(self):
        src = """\
global using System;
global using System.Collections.Generic;

namespace Synth {
    public class Worker { }
}
"""
        r = self._usings(src)
        texts = [t for _, t in r]
        assert any("System" in t for t in texts)


# ══════════════════════════════════════════════════════════════════════════════
# Metadata — usings field
# ══════════════════════════════════════════════════════════════════════════════

class TestUsingsField(unittest.TestCase):

    def test_usings_field_populated(self):
        meta = extract_cs_metadata(USING_SYSTEM_GENERIC.encode())
        assert meta["usings"], f"usings field must not be empty: {meta['usings']}"

    def test_usings_contains_namespace(self):
        meta = extract_cs_metadata(USING_SYSTEM_GENERIC.encode())
        assert any("System" in u for u in meta["usings"])

    def test_no_usings_empty_field(self):
        meta = extract_cs_metadata(NO_USINGS.encode())
        assert meta["usings"] == []

    def test_alias_in_usings_field(self):
        meta = extract_cs_metadata(USING_WITH_ALIAS.encode())
        # The alias directive should appear in usings
        assert any("BS" in u or "Storage" in u for u in meta["usings"]), \
            f"Alias using not in usings field: {meta['usings']}"

    def test_usings_not_in_type_refs(self):
        """Namespaces imported via 'using' must not contaminate type_refs."""
        meta = extract_cs_metadata(USING_SYSTEM_GENERIC.encode())
        # 'Tasks' from 'System.Threading.Tasks' should not pollute type_refs
        # as a standalone type name
        for tr in meta["type_refs"]:
            assert tr not in ("System", "Collections", "Generic", "Threading"), \
                f"Namespace component leaked into type_refs: '{tr}'"


if __name__ == "__main__":
    unittest.main()
