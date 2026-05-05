"""
Integration tests for text (default) mode and declaration fields.

TestSymbolsAndTextModeLive — requires Typesense; tests class_names/method_names end-to-end.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.base import LiveTestBase
from tests.fixtures import (
    CLASS_NAMED_INVENTORYMANAGER, METHOD_NAMED_PROCESSINVENTORY, LITERAL_ONLY,
)
from tests.helpers import _assert_server_ok, _make_git_repo, _delete_collection
from indexserver.config import load_config as _load_config
from indexserver.indexer import run_index

_cfg = _load_config()


class TestSymbolsAndTextModeLive(LiveTestBase):
    """End-to-end symbols and text modes."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp      = int(time.time())
        cls.coll   = f"test_sym_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/InventoryManager.cs": CLASS_NAMED_INVENTORYMANAGER,
            "synth/WarehouseService.cs": METHOD_NAMED_PROCESSINVENTORY,
            "synth/Config.cs":           LITERAL_ONLY,
        })
        run_index(_cfg, src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_declarations_finds_class_name(self):
        fnames = self._ts_search("InventoryManager",
                                 "class_names,method_names,filename")
        assert "InventoryManager.cs" in fnames

    def test_declarations_finds_method_name(self):
        fnames = self._ts_search("ProcessInventory",
                                 "class_names,method_names,filename")
        assert "WarehouseService.cs" in fnames

    def test_declarations_excludes_string_literal_file(self):
        """Config.cs has 'InventoryManager' only in a string — must not match declarations."""
        fnames = self._ts_search("InventoryManager",
                                 "class_names,method_names,filename")
        assert "Config.cs" not in fnames

    def test_text_includes_string_literal_file(self):
        """Text mode includes tokens, so Config.cs IS returned."""
        fnames = self._ts_search("InventoryManager",
                                 "filename,class_names,method_names,tokens")
        assert "Config.cs" in fnames

    def test_text_returns_more_than_declarations(self):
        decl = self._ts_search("InventoryManager",
                               "class_names,method_names,filename",
                               per_page=20)
        text = self._ts_search("InventoryManager",
                               "filename,class_names,method_names,tokens",
                               per_page=20)
        assert len(text) >= len(decl), \
            "text mode must return >= files compared to declaration-field mode"
        assert "Config.cs" in text
        assert "Config.cs" not in decl


if __name__ == "__main__":
    unittest.main()
