"""
Tests for accesses_of mode.

mode: accesses_of MEMBER (q_accesses_of)

Finds every access site of a property or field named MEMBER, regardless of the
receiver type.  Accepts a bare name ("Status") or a dot-qualified name
("Order.Status") to restrict matches to a specific receiver expression prefix.

Gaps tested:
  - Bare name finds all .MEMBER accesses regardless of receiver type.
  - Qualified name restricts matches to the specified receiver prefix.
  - Accesses of a different member on the same receiver are NOT returned.
  - A file with no .MEMBER accesses returns empty.
  - Accesses in comments and string literals are NOT returned.

Run (no Typesense):
    pytest tests/test_mode_accesses_of.py -v
"""
from __future__ import annotations

import unittest

from tests.base import _parse
from tests.fixtures import (
    ACCESSES_OF_STATUS,
    ACCESSES_OF_STATUS_QUALIFIED,
    ACCESSES_OF_NO_STATUS,
)
from src.query.dispatch import q_accesses_of


class TestQAccessesOf(unittest.TestCase):

    def _accesses(self, src, member):
        return q_accesses_of(*_parse(src), member_name=member)

    def test_finds_bare_member_access(self):
        r = self._accesses(ACCESSES_OF_STATUS, "Status")
        assert r, "Expected at least one .Status access"

    def test_finds_all_access_sites(self):
        """order.Status appears twice in ACCESSES_OF_STATUS."""
        r = self._accesses(ACCESSES_OF_STATUS, "Status")
        assert len(r) >= 2, f"Expected 2 .Status accesses, got {len(r)}: {r}"

    def test_different_member_not_returned(self):
        """.Name is accessed but must not appear in .Status results."""
        r = self._accesses(ACCESSES_OF_STATUS, "Status")
        text = " ".join(t for _, t in r)
        assert "Name" not in text, f".Name must not appear in Status results: {r}"

    def test_no_access_returns_empty(self):
        r = self._accesses(ACCESSES_OF_NO_STATUS, "Status")
        assert r == [], f"Expected empty, got {r}"

    def test_qualified_name_restricts_to_receiver(self):
        """order.Status must match, log.Status must not when qualifier is 'order'."""
        r_qual = self._accesses(ACCESSES_OF_STATUS_QUALIFIED, "order.Status")
        assert len(r_qual) == 1, \
            f"Expected 1 match for order.Status, got {len(r_qual)}: {r_qual}"
        text = r_qual[0][1]
        assert "order.Status" in text, f"Match text wrong: {text}"

    def test_bare_name_finds_both_receivers(self):
        """Without qualifier both order.Status and log.Status are returned."""
        r = self._accesses(ACCESSES_OF_STATUS_QUALIFIED, "Status")
        assert len(r) == 2, f"Expected 2 matches for bare Status, got {len(r)}: {r}"

    def test_access_in_comment_not_found(self):
        src = """\
namespace Synth {
    public class C {
        // order.Status - commented
        public void Run(Order order) { }
    }
}
"""
        r = self._accesses(src, "Status")
        assert r == [], "Access in comment must not be returned"

    def test_access_in_string_not_found(self):
        src = """\
namespace Synth {
    public class C {
        public void Run(Order order) {
            string s = "order.Status";
        }
    }
}
"""
        r = self._accesses(src, "Status")
        assert r == [], "Access in string literal must not be returned"


if __name__ == "__main__":
    unittest.main()
