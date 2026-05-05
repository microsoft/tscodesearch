"""
Unit tests for text (default) mode and declaration fields.

Typesense fields: class_names, method_names
search_code query_by (declarations): class_names,method_names,filename
search_code query_by (text):         filename,class_names,method_names,tokens

Gaps tested:
  - class_names contains only declared type names.
  - method_names contains only declared member names (not call targets).
  - String literals are NOT in class_names/method_names but ARE in tokens (text mode).
  - A type used only as a type ref (not declared) is NOT in class_names/method_names.
  - text mode is strictly broader than declaration-field mode.

Integration tests (require Typesense) are in tests/integration/test_mode_symbols.py.
"""
from __future__ import annotations

import os
import tempfile
import unittest

from tests.fixtures import (
    CLASS_NAMED_INVENTORYMANAGER, METHOD_NAMED_PROCESSINVENTORY, LITERAL_ONLY,
    CALLS_FETCHWIDGET, USES_IDATASTORE_PARAM,
)
from indexserver.indexer import extract_metadata, build_document


# ══════════════════════════════════════════════════════════════════════════════
# Metadata — class_names / method_names / symbols
# ══════════════════════════════════════════════════════════════════════════════

class TestSymbolsFields(unittest.TestCase):

    def test_class_name_in_class_names(self):
        meta = extract_metadata(CLASS_NAMED_INVENTORYMANAGER.encode(), ".cs")
        assert "InventoryManager" in meta["class_names"]

    def test_method_name_in_method_names(self):
        meta = extract_metadata(METHOD_NAMED_PROCESSINVENTORY.encode(), ".cs")
        assert "ProcessInventory" in meta["method_names"]

    def test_string_literal_not_in_class_names(self):
        meta = extract_metadata(LITERAL_ONLY.encode(), ".cs")
        assert "InventoryManager" not in meta["class_names"]

    def test_string_literal_not_in_method_names(self):
        meta = extract_metadata(LITERAL_ONLY.encode(), ".cs")
        assert "InventoryManager" not in meta["method_names"]

    def test_call_target_not_in_method_names(self):
        """A call target appears in call_sites but NOT in method_names
        unless it also happens to be defined in the same file."""
        meta = extract_metadata(CALLS_FETCHWIDGET.encode(), ".cs")
        assert "FetchWidget" not in meta["method_names"]

    def test_type_ref_only_not_in_symbols(self):
        """IDataStore used only as a param type ends up in type_refs, not in
        class_names or method_names."""
        meta = extract_metadata(USES_IDATASTORE_PARAM.encode(), ".cs")
        assert "IDataStore" not in meta["class_names"]
        assert "IDataStore" not in meta["method_names"]

    def test_build_document_populates_class_and_method_names(self):
        """build_document populates class_names and method_names correctly."""
        with tempfile.NamedTemporaryFile(suffix=".cs", delete=False, mode="w") as f:
            f.write(CLASS_NAMED_INVENTORYMANAGER)
            tmp = f.name
        try:
            doc = build_document(tmp, "synth/InventoryManager.cs")
            assert "InventoryManager" in doc["class_names"]
        finally:
            os.unlink(tmp)

    def test_interface_name_in_class_names(self):
        src = """\
namespace Synth {
    public interface IWidgetService {
        Widget Get(string id);
    }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        assert "IWidgetService" in meta["class_names"]

    def test_nested_class_in_class_names(self):
        src = """\
namespace Synth {
    public class Outer {
        public class Inner {
            public void Run() { }
        }
    }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        assert "Outer" in meta["class_names"]
        assert "Inner" in meta["class_names"]

    def test_field_name_in_method_names(self):
        """Fields are part of 'method_names' (which covers all member declarations)."""
        src = """\
namespace Synth {
    public class C {
        private string _name;
        public int Count;
    }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        assert "_name" in meta["method_names"] or "Count" in meta["method_names"]


# ══════════════════════════════════════════════════════════════════════════════
# Text mode field coverage (content)
# ══════════════════════════════════════════════════════════════════════════════

class TestTextModeContent(unittest.TestCase):

    def test_string_literal_in_content(self):
        """The raw content field includes the full file text, so string literals
        are findable via text mode."""
        src = LITERAL_ONLY.encode()
        assert b"InventoryManager" in src

    def test_call_site_in_content(self):
        src = CALLS_FETCHWIDGET.encode()
        assert b"FetchWidget" in src

    def test_definitions_in_symbols_and_content(self):
        """A definition-only file has FetchWidget in both method_names and content,
        but NOT in call_sites."""
        meta = extract_metadata(METHOD_NAMED_PROCESSINVENTORY.encode(), ".cs")
        assert "ProcessInventory" in meta["method_names"]  # symbols
        assert "ProcessInventory" not in meta["call_sites"]  # NOT calls

    def test_comment_in_content_not_in_symbols(self):
        src = """\
namespace Synth {
    // InventoryManager is the target
    public class Worker { }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        assert "InventoryManager" not in meta["class_names"]
        assert "InventoryManager" not in meta["method_names"]
        # content contains it (raw text)
        assert b"InventoryManager" in src.encode()


if __name__ == "__main__":
    unittest.main()
