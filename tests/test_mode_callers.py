"""
Tests for calls mode.

Typesense field: call_sites
search_code mode: calls  (query_by: call_sites,filename)
mode: calls METHOD (q_calls)

Gaps tested:
  - A definition file must NOT appear in calls results (no call_sites entry for
    the method it defines).
  - A callers file MUST appear (call sites indexed).
  - Constructor calls (new T()) are indexed in call_sites.
  - Duplicate calls to the same method are deduplicated in call_sites.
  - q_calls supports dot-qualified names (Receiver.Method) to restrict matches.

Run (no Typesense):
    pytest tests/test_mode_callers.py -v -k "not Live"
Run (with Typesense):
    pytest tests/test_mode_callers.py -v
"""
from __future__ import annotations

import shutil
import time
import unittest

from tests.base import _parse, LiveTestBase
from tests.fixtures import (
    CALLS_FETCHWIDGET, DEFINES_FETCHWIDGET, CALLS_IBLOBSERVICE,
    CALLS_WIDGET_CTOR, DEFINES_WIDGET_CTOR,
)
from tests.helpers import _assert_server_ok, _make_git_repo, _delete_collection
from indexserver.indexer import extract_metadata, run_index
from query.cs import q_calls


# ══════════════════════════════════════════════════════════════════════════════
# Metadata — call_sites field
# ══════════════════════════════════════════════════════════════════════════════

class TestCallSitesField(unittest.TestCase):
    """extract_metadata correctly populates call_sites."""

    def test_call_appears_in_call_sites(self):
        meta = extract_metadata(CALLS_FETCHWIDGET.encode(), ".cs")
        assert "FetchWidget" in meta["call_sites"], \
            f"call_sites: {meta['call_sites']}"

    def test_definition_not_in_call_sites(self):
        meta = extract_metadata(DEFINES_FETCHWIDGET.encode(), ".cs")
        assert "FetchWidget" not in meta["call_sites"], \
            "Definition must not appear in call_sites"

    def test_definition_in_method_names(self):
        meta = extract_metadata(DEFINES_FETCHWIDGET.encode(), ".cs")
        assert "FetchWidget" in meta["method_names"]

    def test_definition_in_member_sigs(self):
        meta = extract_metadata(DEFINES_FETCHWIDGET.encode(), ".cs")
        assert any("FetchWidget" in s for s in meta["member_sigs"])

    def test_callers_file_not_in_member_sigs(self):
        """The caller does not define FetchWidget — must not be in member_sigs."""
        meta = extract_metadata(CALLS_FETCHWIDGET.encode(), ".cs")
        assert not any("FetchWidget" in s for s in meta["member_sigs"]), \
            "FetchWidget must not appear in member_sigs of callers file"

    def test_duplicate_calls_deduped(self):
        """FetchWidget called twice — should appear once in call_sites."""
        meta = extract_metadata(CALLS_FETCHWIDGET.encode(), ".cs")
        count = meta["call_sites"].count("FetchWidget")
        assert count == 1, f"Expected 1 occurrence in call_sites, got {count}"

    def test_constructor_call_in_call_sites(self):
        """new Widget(id) is an invocation — must be in call_sites."""
        meta = extract_metadata(CALLS_WIDGET_CTOR.encode(), ".cs")
        assert "Widget" in meta["call_sites"], \
            f"Constructor call not in call_sites: {meta['call_sites']}"

    def test_constructor_definition_not_in_call_sites(self):
        meta = extract_metadata(DEFINES_WIDGET_CTOR.encode(), ".cs")
        assert "Widget" not in meta["call_sites"], \
            "Constructor definition must not be in call_sites"

    def test_unrelated_file_empty_call_sites(self):
        """A file with no method calls has no relevant call_sites entries."""
        src = """\
namespace Synth {
    public class EmptyClass { }
}
"""
        meta = extract_metadata(src.encode(), ".cs")
        assert "FetchWidget" not in meta["call_sites"]


# ══════════════════════════════════════════════════════════════════════════════
# q_calls AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQCalls(unittest.TestCase):
    """q_calls finds call sites correctly."""

    def _calls(self, src, method):
        return q_calls(*_parse(src), method_name=method)

    def test_finds_member_call(self):
        r = self._calls(CALLS_FETCHWIDGET, "FetchWidget")
        assert r, "Expected at least one FetchWidget call site"

    def test_both_call_sites_found(self):
        """FetchWidget is called twice — both should appear."""
        r = self._calls(CALLS_FETCHWIDGET, "FetchWidget")
        assert len(r) >= 2, f"Expected 2 call sites, got {len(r)}"

    def test_definition_file_not_returned(self):
        r = self._calls(DEFINES_FETCHWIDGET, "FetchWidget")
        assert r == [], "q_calls on definition file must return empty"

    def test_constructor_call_found(self):
        r = self._calls(CALLS_WIDGET_CTOR, "Widget")
        assert r, "new Widget(id) must appear in q_calls"

    def test_constructor_definition_not_returned(self):
        r = self._calls(DEFINES_WIDGET_CTOR, "Widget")
        assert r == [], "Constructor definition must not appear in q_calls"

    def test_unrelated_call_not_returned(self):
        r = self._calls(CALLS_FETCHWIDGET, "SaveWidget")
        assert r == [], "SaveWidget not called in caller file — must be empty"

    def test_qualified_name_restricts_match(self):
        """Qualifier '_ws.FetchWidget' must match, 'other.FetchWidget' must not."""
        src = """\
namespace Synth {
    public class Mixed {
        private IWidgetService _ws;
        private IWidgetService _other;
        public void Run() {
            _ws.FetchWidget("a");
            _other.FetchWidget("b");
        }
    }
}
"""
        # Without qualifier: both calls
        r_bare = self._calls(src, "FetchWidget")
        assert len(r_bare) == 2

        # With qualifier: only the _ws call
        r_qual = self._calls(src, "_ws.FetchWidget")
        assert len(r_qual) == 1

    def test_call_in_comment_not_found(self):
        src = """\
namespace Synth {
    public class Worker {
        // FetchWidget("x") - commented out
        public void Run() { }
    }
}
"""
        r = self._calls(src, "FetchWidget")
        assert r == [], "Call in comment must not be returned by q_calls"

    def test_call_in_string_not_found(self):
        src = """\
namespace Synth {
    public class Worker {
        public void Run() {
            string s = \"FetchWidget(x)\";
        }
    }
}
"""
        r = self._calls(src, "FetchWidget")
        assert r == []


# ══════════════════════════════════════════════════════════════════════════════
# Live integration
# ══════════════════════════════════════════════════════════════════════════════

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
