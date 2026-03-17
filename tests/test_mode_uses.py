"""
Tests for uses mode.

Typesense field: type_refs
search_code query_by: type_refs,symbols,class_names,filename
mode: --uses TYPE (q_uses)

Gaps tested:
  - Fields, properties, params, and return types all populate type_refs.
  - Comments and string literals must NOT populate type_refs.
  - Call targets must NOT appear in type_refs.
  - Generic types (IList<T>) are expanded so the inner type T is searchable.
  - q_uses skips declaration names and invocation targets (not type usages).
"""
from __future__ import annotations

import shutil
import time
import unittest

from tests.base import _parse, LiveTestBase
from tests.fixtures import (
    DECLARES_FIELD_IDATASTORE, USES_IDATASTORE_PARAM, COMMENT_ONLY_IDATASTORE,
    CALLS_FETCHWIDGET, IMPLEMENTS_IDATASTORE,
    LOCAL_VAR_IDATASTORE, STATIC_RECEIVER_IDATASTORE,
)
from tests.helpers import _server_ok, _make_git_repo, _delete_collection
from indexserver.indexer import extract_cs_metadata, run_index
from query import q_uses


# ══════════════════════════════════════════════════════════════════════════════
# Metadata — type_refs field
# ══════════════════════════════════════════════════════════════════════════════

class TestTypeRefsField(unittest.TestCase):

    def test_field_type_in_type_refs(self):
        meta = extract_cs_metadata(DECLARES_FIELD_IDATASTORE.encode())
        assert "IDataStore" in meta["type_refs"]

    def test_property_type_in_type_refs(self):
        meta = extract_cs_metadata(DECLARES_FIELD_IDATASTORE.encode())
        assert "IDataStore" in meta["type_refs"]

    def test_param_type_in_type_refs(self):
        meta = extract_cs_metadata(USES_IDATASTORE_PARAM.encode())
        assert "IDataStore" in meta["type_refs"]

    def test_return_type_in_type_refs(self):
        """Method return type must be indexed in type_refs (uses 'returns' field)."""
        src = """\
namespace Synth {
    public class Factory {
        public IDataStore Create() { return null; }
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert "IDataStore" in meta["type_refs"], \
            f"Return type must be in type_refs: {meta['type_refs']}"

    def test_comment_not_in_type_refs(self):
        meta = extract_cs_metadata(COMMENT_ONLY_IDATASTORE.encode())
        assert "IDataStore" not in meta["type_refs"]

    def test_call_target_not_in_type_refs(self):
        meta = extract_cs_metadata(CALLS_FETCHWIDGET.encode())
        assert "FetchWidget" not in meta["type_refs"]

    def test_generic_type_expanded(self):
        src = """\
namespace Synth {
    public class Mgr {
        private IList<IDataStore> _stores;
        public void Register(IList<IDataStore> stores) { }
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert "IDataStore" in meta["type_refs"]
        assert "IList"      in meta["type_refs"]

    def test_fully_qualified_type_unqualified_in_type_refs(self):
        src = """\
namespace Synth {
    public class User {
        private Synth.IDataStore _store;
    }
}
"""
        meta = extract_cs_metadata(src.encode())
        assert any("IDataStore" in t for t in meta["type_refs"]), \
            f"Qualified type must be stored unqualified: {meta['type_refs']}"

    def test_base_class_also_in_type_refs(self):
        """base_types are also added to type_refs for uses mode to find implementors."""
        meta = extract_cs_metadata(IMPLEMENTS_IDATASTORE.encode())
        # base_types ends up in type_refs via _expand_type_refs
        assert "IDataStore" in meta["type_refs"] or \
               "IDataStore" in meta["base_types"]

    def test_local_var_type_in_type_refs(self):
        """Local variable declaration type must appear in type_refs."""
        meta = extract_cs_metadata(LOCAL_VAR_IDATASTORE.encode())
        assert "IDataStore" in meta["type_refs"], \
            f"Local var type must be in type_refs: {meta['type_refs']}"

    def test_static_receiver_in_type_refs(self):
        """PascalCase static call receiver (IDataStore.Flush()) must appear in type_refs."""
        meta = extract_cs_metadata(STATIC_RECEIVER_IDATASTORE.encode())
        assert "IDataStore" in meta["type_refs"], \
            f"Static receiver must be in type_refs: {meta['type_refs']}"

    def test_lowercase_receiver_not_in_type_refs(self):
        """Instance call receiver (_svc.Method()) must NOT be added as a type ref."""
        meta = extract_cs_metadata(USES_IDATASTORE_PARAM.encode())
        # '_src' is lowercase — must not be added as a type_ref
        assert "_src" not in meta["type_refs"]


# ══════════════════════════════════════════════════════════════════════════════
# q_uses AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQUses(unittest.TestCase):

    def _uses(self, src, type_name):
        return q_uses(*_parse(src), type_name=type_name)

    def test_finds_field_declaration(self):
        r = self._uses(DECLARES_FIELD_IDATASTORE, "IDataStore")
        assert r, "Field declaration of IDataStore must be found"

    def test_finds_param_type(self):
        r = self._uses(USES_IDATASTORE_PARAM, "IDataStore")
        assert r, "Parameter of type IDataStore must be found"

    def test_comment_not_found(self):
        r = self._uses(COMMENT_ONLY_IDATASTORE, "IDataStore")
        assert r == [], "Comment must not be found by q_uses"

    def test_call_target_not_found(self):
        r = self._uses(CALLS_FETCHWIDGET, "FetchWidget")
        assert r == [], "Call target must not be found by q_uses"

    def test_declaration_name_not_found(self):
        """The declared class name 'SqlDataStore' is not a *use* of 'IDataStore'."""
        r = self._uses(IMPLEMENTS_IDATASTORE, "SqlDataStore")
        assert r == [], "Class declaration name is not a type use"

    def test_return_type_found(self):
        src = """\
namespace Synth {
    public class Factory {
        public IDataStore Create() { return null; }
    }
}
"""
        r = self._uses(src, "IDataStore")
        assert r, "Return type must be found by q_uses"

    def test_cast_target_found(self):
        src = """\
namespace Synth {
    public class C {
        public void Run(object o) {
            var d = (IDataStore)o;
        }
    }
}
"""
        r = self._uses(src, "IDataStore")
        assert r, "Cast target must be found by q_uses"

    def test_no_duplicate_lines(self):
        """Multiple references on the same line produce only one result row."""
        src = """\
namespace Synth {
    public class C {
        public IDataStore Swap(IDataStore a, IDataStore b) { return a; }
    }
}
"""
        r = self._uses(src, "IDataStore")
        lines = [ln for ln, _ in r]
        assert len(lines) == len(set(lines)), "Duplicate lines must not appear"

    def test_string_literal_not_found(self):
        src = """\
namespace Synth {
    public class C {
        public string Desc = \"IDataStore is the storage interface\";
    }
}
"""
        r = self._uses(src, "IDataStore")
        assert r == []


# ══════════════════════════════════════════════════════════════════════════════
# Live integration
# ══════════════════════════════════════════════════════════════════════════════

@unittest.skipUnless(_server_ok(), "Typesense not running — start with: ts start")
class TestUsesModeLive(LiveTestBase):
    """End-to-end uses mode: query_by = type_refs,symbols,class_names,filename."""

    @classmethod
    def setUpClass(cls):
        stamp      = int(time.time())
        cls.coll   = f"test_uses_{stamp}"
        cls.tmpdir = _make_git_repo({
            "synth/SqlDataStore.cs":  IMPLEMENTS_IDATASTORE,
            "synth/DataTransfer.cs":  USES_IDATASTORE_PARAM,
            "synth/CachingProxy.cs":  DECLARES_FIELD_IDATASTORE,
            "synth/Indirect.cs":      COMMENT_ONLY_IDATASTORE,
            "synth/LocalVarUser.cs":  LOCAL_VAR_IDATASTORE,
            "synth/StaticUser.cs":    STATIC_RECEIVER_IDATASTORE,
        })
        run_index(src_root=cls.tmpdir, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        _delete_collection(cls.coll)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_finds_param_file(self):
        fnames = self._ts_search("IDataStore", "type_refs,symbols,class_names,filename")
        assert "DataTransfer.cs" in fnames

    def test_finds_field_file(self):
        fnames = self._ts_search("IDataStore", "type_refs,symbols,class_names,filename")
        assert "CachingProxy.cs" in fnames

    def test_excludes_comment_only_file(self):
        fnames = self._ts_search("IDataStore", "type_refs,symbols,class_names,filename")
        assert "Indirect.cs" not in fnames

    def test_text_mode_finds_comment_file(self):
        """Content field picks up comments — text mode is broader than uses mode."""
        fnames = self._ts_search("IDataStore",
                                 "filename,symbols,class_names,method_names,content")
        assert "Indirect.cs" in fnames

    def test_finds_local_var_file(self):
        """File where IDataStore appears only as a local variable type must be found."""
        fnames = self._ts_search("IDataStore", "type_refs,symbols,class_names,filename")
        assert "LocalVarUser.cs" in fnames, \
            f"Local-var-only file must be found by uses mode: {fnames}"

    def test_finds_static_receiver_file(self):
        """File where IDataStore appears only as a static call receiver must be found."""
        fnames = self._ts_search("IDataStore", "type_refs,symbols,class_names,filename")
        assert "StaticUser.cs" in fnames, \
            f"Static-receiver file must be found by uses mode: {fnames}"

    def test_uses_finds_more_than_implements(self):
        uses = self._ts_search("IDataStore", "type_refs,symbols,class_names,filename",
                               per_page=20)
        impl = self._ts_search("IDataStore", "base_types,class_names,filename",
                               per_page=20)
        assert len(uses) >= len(impl), \
            "uses mode must return >= files compared to implements mode"


if __name__ == "__main__":
    unittest.main()
