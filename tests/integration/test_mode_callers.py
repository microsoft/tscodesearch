"""
Integration tests for calls mode.

TestCallersModeLive — requires Typesense; tests call_sites field end-to-end.
"""
from __future__ import annotations
import os, sys, shutil, time, unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.base import LiveTestBase
from tests.fixtures import (
    CALLS_FETCHWIDGET, DEFINES_FETCHWIDGET, CALLS_IBLOBSERVICE,
)
from tests.helpers import _assert_server_ok, _make_git_repo, _delete_collection
from indexserver.indexer import run_index


class TestCallersModeLive(LiveTestBase):
    """End-to-end calls mode: query_by = call_sites,filename."""

    @classmethod
    def setUpClass(cls):
        _assert_server_ok()
        stamp      = int(time.time())
        cls.coll   = f"test_callers_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/WidgetClient.cs":  CALLS_FETCHWIDGET,
            "synth/WidgetService.cs": DEFINES_FETCHWIDGET,
            "synth/Reporter.cs":      CALLS_IBLOBSERVICE,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_callers_finds_caller_file(self):
        fnames = self._ts_search("FetchWidget", "call_sites,filename")
        assert "WidgetClient.cs" in fnames

    def test_callers_excludes_definition_file(self):
        fnames = self._ts_search("FetchWidget", "call_sites,filename")
        assert "WidgetService.cs" not in fnames, \
            "Definition-only file must not appear in callers search"

    def test_callers_excludes_unrelated_file(self):
        fnames = self._ts_search("FetchWidget", "call_sites,filename")
        assert "Reporter.cs" not in fnames

    def test_text_mode_finds_definition_file(self):
        """Text mode returns definition file (FetchWidget in method_names/tokens)."""
        fnames = self._ts_search("FetchWidget",
                                 "filename,class_names,method_names,tokens")
        assert "WidgetService.cs" in fnames

    def test_text_mode_also_finds_caller_file(self):
        fnames = self._ts_search("FetchWidget",
                                 "filename,class_names,method_names,tokens")
        assert "WidgetClient.cs" in fnames

    def test_calls_fewer_results_than_text(self):
        """calls mode must return <= files compared to text mode."""
        callers = self._ts_search("FetchWidget", "call_sites,filename", per_page=20)
        text    = self._ts_search("FetchWidget",
                                  "filename,class_names,method_names,tokens",
                                  per_page=20)
        assert len(callers) <= len(text)


if __name__ == "__main__":
    unittest.main()
