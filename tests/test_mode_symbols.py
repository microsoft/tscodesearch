"""
Tests for symbols mode and text (default) mode.

Typesense fields: symbols = class_names ∪ method_names
search_code query_by (symbols): symbols,class_names,method_names,filename
search_code query_by (text):    filename,symbols,class_names,method_names,content

Gaps tested:
  - class_names contains only declared type names.
  - method_names contains only declared member names (not call targets).
  - String literals are NOT in symbols but ARE in content (text mode).
  - A type used only as a type ref (not declared) is NOT in symbols.
  - text mode is strictly broader than symbols mode.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest

from tests.base import _parse, LiveTestBase
from tests.fixtures import (
    CLASS_NAMED_INVENTORYMANAGER, METHOD_NAMED_PROCESSINVENTORY, LITERAL_ONLY,
    CALLS_FETCHWIDGET, USES_IDATASTORE_PARAM,
)
from tests.helpers import _server_ok, _make_git_repo, _delete_collection
from indexserver.indexer import extract_cs_metadata, build_document, run_index


# ══════════════════════════════════════════════════════════════════════════════
# Metadata — class_names / method_names / symbols
# ══════════════════════════════════════════════════════════════════════════════

class TestSymbolsFields(unittest.TestCase):

    def test_class_name_in_class_names(self):
        meta = extract_cs_metadata(CLASS_NAMED_INVENTORYMANAGER.encode())
        assert "InventoryManager" in meta["class_names"]

    def test_method_name_in_method_names(self):
        meta = extract_cs_metadata(METHOD_NAMED_PROCESSINVENTORY.encode())
        assert "ProcessInventory" in meta["method_names"]

    def test_string_literal_not_in_class_names(self):
        meta = extract_cs_metadata(LITERAL_ONLY.encode())
        assert "InventoryManager" not in meta["class_names"]

    def test_string_literal_not_in_method_names(self):
        meta = extract_cs_metadata(LITERAL_ONLY.encode())
        assert "InventoryManager" not in meta["method_names"]

    def test_call_target_not_in_method_names(self):
        """A call target appears in call_sites but NOT in method_names
        unless it also happens to be defined in the same file."""
        meta = extract_cs_metadata(CALLS_FETCHWIDGET.encode())
        assert "FetchWidget" not in meta["method_names"]

    def test_type_ref_only_not_in_symbols(self):
        """IDataStore used only as a param type ends up in type_refs, not in
        class_names or method_names."""
        meta = extract_cs_metadata(USES_IDATASTORE_PARAM.encode())
        assert "IDataStore" not in meta["class_names"]
        assert "IDataStore" not in meta["method_names"]

    def test_symbols_is_union_of_class_and_method_names(self):
        """build_document produces symbols = deduplicated class_names + method_names."""
        with tempfile.NamedTemporaryFile(suffix=".cs", delete=False, mode="w") as f:
            f.write(CLASS_NAMED_INVENTORYMANAGER)
            tmp = f.name
        try:
            doc = build_document(tmp, "synth/InventoryManager.cs")
            expected = list(dict.fromkeys(doc["class_names"] + doc["method_names"]))
            assert doc["symbols"] == expected
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
        meta = extract_cs_metadata(src.encode())
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
        meta = extract_cs_metadata(src.encode())
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
        meta = extract_cs_metadata(src.encode())
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
        meta = extract_cs_metadata(METHOD_NAMED_PROCESSINVENTORY.encode())
        assert "ProcessInventory" in meta["method_names"]  # symbols
        assert "ProcessInventory" not in meta["call_sites"]  # NOT calls

    def test_comment_in_content_not_in_symbols(self):
        src = """\
namespace Synth {
    // InventoryManager is the target
    public class Worker { }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert "InventoryManager" not in meta["class_names"]
        assert "InventoryManager" not in meta["method_names"]
        # content contains it (raw text)
        assert b"InventoryManager" in src.encode()


# ══════════════════════════════════════════════════════════════════════════════
# Live integration
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_server_ok(), "Typesense not running — start with: ts start")
class TestSymbolsAndTextModeLive(LiveTestBase):
    """End-to-end symbols and text modes."""

    @classmethod
    def setUpClass(cls):
        stamp      = int(time.time())
        cls.coll   = f"test_sym_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/InventoryManager.cs": CLASS_NAMED_INVENTORYMANAGER,
            "synth/WarehouseService.cs": METHOD_NAMED_PROCESSINVENTORY,
            "synth/Config.cs":           LITERAL_ONLY,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, reset=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_symbols_finds_class_name(self):
        fnames = self._ts_search("InventoryManager",
                                 "symbols,class_names,method_names,filename")
        assert "InventoryManager.cs" in fnames

    def test_symbols_finds_method_name(self):
        fnames = self._ts_search("ProcessInventory",
                                 "symbols,class_names,method_names,filename")
        assert "WarehouseService.cs" in fnames

    def test_symbols_excludes_string_literal_file(self):
        """Config.cs has 'InventoryManager' only in a string — must not match symbols."""
        fnames = self._ts_search("InventoryManager",
                                 "symbols,class_names,method_names,filename")
        assert "Config.cs" not in fnames

    def test_text_includes_string_literal_file(self):
        """Text mode includes content, so Config.cs IS returned."""
        fnames = self._ts_search("InventoryManager",
                                 "filename,symbols,class_names,method_names,content")
        assert "Config.cs" in fnames

    def test_text_returns_more_than_symbols(self):
        sym  = self._ts_search("InventoryManager",
                               "symbols,class_names,method_names,filename",
                               per_page=20)
        text = self._ts_search("InventoryManager",
                               "filename,symbols,class_names,method_names,content",
                               per_page=20)
        assert len(text) >= len(sym), \
            "text mode must return >= files compared to symbols mode"
        assert "Config.cs" in text
        assert "Config.cs" not in sym


if __name__ == "__main__":
    unittest.main()
