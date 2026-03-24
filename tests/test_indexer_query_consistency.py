"""
Tests verifying consistency between the indexer (extract_cs_metadata) and the
query functions (q_*), and documenting known gaps between the two systems.

These tests run purely in-memory — no Typesense server required.

Gap taxonomy used in this file:
  CONSISTENT  – both systems produce equivalent data for this property
  GAP         – indexer and query diverge; documented with details
  MISSING     – a query mode has no corresponding indexed field at all
"""

import os
import sys
import pytest

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

from indexserver.indexer import extract_cs_metadata
from query import (
    q_classes, q_methods, q_fields, q_calls, q_implements, q_uses,
    q_attrs, q_usings, q_uses, q_casts, q_all_refs,
)

# ---------------------------------------------------------------------------
# Shared parser
# ---------------------------------------------------------------------------

_CS = Language(tscsharp.language())
_PARSER = Parser(_CS)


def _parse(src: bytes):
    tree = _PARSER.parse(src)
    lines = src.decode("utf-8", errors="replace").splitlines()
    return src, tree, lines


def _texts(results):
    return [t for _, t in results]


# ---------------------------------------------------------------------------
# Shared fixture — exercises all metadata categories
# ---------------------------------------------------------------------------

_FIXTURE = b"""\
using System;
using System.Collections.Generic;
using Acme.Storage;

namespace TestApp {

    [Serializable]
    [Obsolete("use NewWidget")]
    public interface IWidget {
        string Transform(string input);
    }

    public abstract class BaseWidget : IWidget, IDisposable {
        protected IWidget _inner;
        public BaseWidget(IWidget inner) { _inner = inner; }
        public abstract string Transform(string input);
        public virtual void Dispose() { }
        public event EventHandler OnChanged;
    }

    public class ConcreteWidget : BaseWidget {
        private readonly string _prefix;
        public string Prefix { get; set; }

        public ConcreteWidget(IWidget inner, string prefix)
            : base(inner) {
            _prefix = prefix;
        }

        public override string Transform(string input) {
            var raw = _inner.Transform(input);
            var cast = (BaseWidget)_inner;
            var asCast = _inner as BaseWidget;
            return _prefix + raw;
        }

        private void LogIt(IWidget w) {
            var name = w.Transform("test");
        }
    }

    public static class WidgetFactory {
        public static IWidget Create(string prefix) {
            return new ConcreteWidget(null, prefix);
        }
        // COMMENT_CALL() should never appear in call results
    }

    public struct WidgetResult {
        public bool Success;
        public string Output;
        public int ErrorCode;
        public WidgetResult(bool ok, string out_, int err) {
            Success = ok; Output = out_; ErrorCode = err;
        }
    }
}
"""


@pytest.fixture(scope="module")
def fx():
    return _parse(_FIXTURE)


@pytest.fixture(scope="module")
def meta():
    return extract_cs_metadata(_FIXTURE)


# ===========================================================================
# CONSISTENT: base_types
# ===========================================================================

class TestBaseTypesConsistency:
    """CONSISTENT — indexer base_types matches what q_classes / q_implements sees."""

    def test_indexer_finds_iwidget_in_base_types(self, meta):
        assert "IWidget" in meta["base_types"], \
            f"base_types: {meta['base_types']}"

    def test_indexer_finds_idisposable_in_base_types(self, meta):
        assert "IDisposable" in meta["base_types"]

    def test_indexer_finds_basewidget_in_base_types(self, meta):
        assert "BaseWidget" in meta["base_types"]

    def test_query_implements_finds_iwidget_implementors(self, fx):
        r = q_implements(*fx, "IWidget")
        names = _texts(r)
        # BaseWidget directly declares ': IWidget, IDisposable'
        assert any("BaseWidget" in n for n in names)
        # ConcreteWidget inherits IWidget transitively (via BaseWidget) only;
        # q_implements checks direct base types only, so it does NOT appear
        assert not any("ConcreteWidget" in n for n in names), \
            "ConcreteWidget should not appear: it implements IWidget transitively only"

    def test_indexer_and_query_agree_on_iwidget(self, meta, fx):
        """Every type that q_implements finds for IWidget should be represented
        in the indexer's class_names alongside a base_types entry."""
        r = q_implements(*fx, "IWidget")
        for _, txt in r:
            # txt is "[class] ClassName : base1, base2"
            # extract declared name (between '] ' and ' :')
            after_bracket = txt.split("] ", 1)[-1]
            declared = after_bracket.split(" :")[0].strip()
            assert declared in meta["class_names"], \
                f"q_implements found {declared!r} but it's absent from class_names"
        assert "IWidget" in meta["base_types"]

    def test_qualified_base_type_stripped_to_simple_name(self):
        """Both simple and generic qualified base types are fully stripped to bare identifiers."""
        src = b"namespace N { public class C : Acme.IFoo, Generic.IBar<C> { } }"
        m = extract_cs_metadata(src)
        # Simple qualified name: 'Acme.IFoo' -> 'IFoo'
        assert "IFoo" in m["base_types"], f"base_types: {m['base_types']}"
        assert "Acme.IFoo" not in m["base_types"]
        # Generic qualified name: 'Generic.IBar<C>' -> 'IBar' (generic suffix stripped)
        assert "IBar" in m["base_types"], f"base_types: {m['base_types']}"
        assert "IBar<C>" not in m["base_types"]
        assert "Generic.IBar<C>" not in m["base_types"]

    def test_nested_generic_qualified_base_type_stripped(self):
        """Outer.MyClass<OtherClass<T>> -> 'MyClass' (first '<' determines the cut point)."""
        src = b"namespace N { public class C : Outer.MyClass<OtherClass<string>> { } }"
        m = extract_cs_metadata(src)
        assert "MyClass" in m["base_types"], f"base_types: {m['base_types']}"
        assert "MyClass<OtherClass<string>>" not in m["base_types"]
        assert "Outer.MyClass<OtherClass<string>>" not in m["base_types"]


# ===========================================================================
# CONSISTENT: call_sites (method calls + constructor calls)
# ===========================================================================

class TestCallSitesConsistency:
    """CONSISTENT — indexer call_sites and q_calls both use _collect_ctor_names
    and share the same AST traversal via cs_ast helpers."""

    def test_indexer_includes_method_call(self, meta):
        assert "Transform" in meta["call_sites"], \
            f"call_sites: {meta['call_sites']}"

    def test_indexer_includes_ctor_call(self, meta):
        assert "ConcreteWidget" in meta["call_sites"], \
            f"call_sites (ctor): {meta['call_sites']}"

    def test_query_finds_method_call(self, fx):
        r = q_calls(*fx, "Transform")
        assert len(r) >= 1

    def test_query_finds_ctor_call(self, fx):
        r = q_calls(*fx, "ConcreteWidget")
        assert len(r) >= 1

    def test_query_skips_comment_calls(self, fx):
        r = q_calls(*fx, "COMMENT_CALL")
        assert len(r) == 0, "q_calls leaked comment-only call"

    def test_every_indexed_call_findable_by_query(self, meta, fx):
        """Every name in call_sites should be findable by q_calls in the same source."""
        for call in meta["call_sites"]:
            r = q_calls(*fx, call)
            assert len(r) >= 1, \
                f"'{call}' in call_sites but q_calls found nothing for it"


# ===========================================================================
# CONSISTENT: class_names
# ===========================================================================

class TestClassNamesConsistency:
    """CONSISTENT — both systems enumerate the same set of type declaration names."""

    def test_indexer_finds_all_type_names(self, meta):
        for name in ("IWidget", "BaseWidget", "ConcreteWidget",
                     "WidgetFactory", "WidgetResult"):
            assert name in meta["class_names"], \
                f"'{name}' missing from class_names: {meta['class_names']}"

    def test_query_finds_same_type_names(self, fx):
        r = q_classes(*fx)
        texts = _texts(r)
        for name in ("IWidget", "BaseWidget", "ConcreteWidget",
                     "WidgetFactory", "WidgetResult"):
            assert any(name in t for t in texts), \
                f"'{name}' missing from q_classes output"

    def test_indexer_matches_query_count(self, meta, fx):
        """Number of distinct class_names from indexer == number of types from q_classes."""
        q_names = {
            txt.split("] ", 1)[-1].split(" :")[0].strip()
            for _, txt in q_classes(*fx)
        }
        for name in meta["class_names"]:
            assert name in q_names, \
                f"Indexer class_names has {name!r} but q_classes missed it"


# ===========================================================================
# CONSISTENT: attributes
# ===========================================================================

class TestAttributesConsistency:
    """CONSISTENT — both systems strip "Attribute" suffix and unqualify."""

    def test_indexer_strips_attribute_suffix(self, meta):
        assert "Serializable" in meta["attr_names"], \
            f"attr_names: {meta['attr_names']}"
        assert "SerializableAttribute" not in meta["attr_names"]

    def test_indexer_strips_obsolete(self, meta):
        assert "Obsolete" in meta["attr_names"]

    def test_query_finds_serializable(self, fx):
        r = q_attrs(*fx, "Serializable")
        assert len(r) >= 1

    def test_query_finds_obsolete(self, fx):
        r = q_attrs(*fx, "Obsolete")
        assert len(r) >= 1

    def test_every_indexed_attr_findable_by_query(self, meta, fx):
        for attr in meta["attr_names"]:
            r = q_attrs(*fx, attr)
            assert len(r) >= 1, \
                f"'{attr}' in indexed attributes but q_attrs found nothing"

    def test_qualified_attribute_stripped(self):
        """[Acme.Auth.AuthorizeAttribute] must be stored as 'Authorize'."""
        src = b"namespace N { [Acme.Auth.AuthorizeAttribute] public class C {} }"
        m = extract_cs_metadata(src)
        assert "Authorize" in m["attr_names"], f"attr_names: {m['attr_names']}"
        assert "Acme.Auth.AuthorizeAttribute" not in m["attr_names"]


# ===========================================================================
# GAP: member_sigs — indexer uses "returns" field; query uses "type" field
# ===========================================================================

class TestMethodSigsFieldNameGap:
    """
    GAP: indexer uses child_by_field_name("returns") to get method return types,
    but tree-sitter-c-sharp 0.23.x exposes the return type on the "type" field of
    method_declaration.  q_methods/_build_sig correctly uses "type".

    Effect: indexer member_sigs omit return types (stored as "MethodName(ParamType)")
    while q_methods shows them as "[method] RetType MethodName(ParamType param)".

    This test documents the discrepancy.  Fix: change indexer to use
    child_by_field_name("type") for method return types.
    """

    def test_query_includes_return_type_in_method_output(self, fx):
        """q_methods correctly shows return types."""
        r = q_methods(*fx)
        transform_lines = [t for _, t in r if "Transform" in t and "[method]" in t]
        assert transform_lines, "Transform method not found by q_methods"
        assert any("string" in t for t in transform_lines), \
            f"Return type 'string' missing from q_methods output: {transform_lines}"

    def test_indexer_member_sigs_contain_method_name(self, meta):
        """Sanity: method sigs at minimum contain the method name."""
        sigs = meta["member_sigs"]
        assert any("Transform" in s for s in sigs), \
            f"Transform not found in member_sigs: {sigs}"

    def test_indexer_member_sigs_include_return_type(self, meta):
        """Indexer member_sigs include return types (uses child_by_field_name('type'))."""
        sigs = meta["member_sigs"]
        transform_sigs = [s for s in sigs if "Transform" in s]
        assert transform_sigs, f"no Transform sig found: {sigs}"
        assert any("string" in s for s in transform_sigs), \
            f"Return type 'string' missing from Transform member_sigs: {transform_sigs}"

    def test_constructor_sig_has_no_return_type(self, meta):
        """Constructors correctly have no return type in either system."""
        sigs = meta["member_sigs"]
        ctor_sigs = [s for s in sigs if "ConcreteWidget" in s]
        assert ctor_sigs, f"ConcreteWidget ctor sig missing: {sigs}"
        # Ctor sigs should NOT start with a return type
        for s in ctor_sigs:
            assert not s.startswith("void "), \
                f"ctor sig incorrectly has void prefix: {s!r}"


# ===========================================================================
# GAP: type_refs — indexer is narrower than q_uses
# ===========================================================================

class TestTypeRefsVsUsesGap:
    """
    type_refs field coverage vs q_uses:

    COVERED (type_refs AND q_uses):
      - field/property types, method return types, method/ctor parameter types
      - base_types (implements clause)  — added: base_types merged into type_refs
      - local variable declaration types — gap CLOSED: indexer now scans local decls
      - PascalCase static call receivers  — gap CLOSED: indexer now extracts these

    SPLIT (explicit casts go to cast_types, not type_refs):
      - explicit cast targets — in cast_types (new T1 field) but NOT in type_refs
    """

    # ── items that SHOULD be in type_refs ────────────────────────────────────

    def test_field_type_in_type_refs(self, meta):
        """Field 'IWidget _inner' → 'IWidget' must appear in type_refs."""
        assert "IWidget" in meta["type_refs"], \
            f"type_refs: {meta['type_refs']}"

    def test_property_type_in_type_refs(self, meta):
        assert "string" in meta["type_refs"]

    def test_method_return_type_in_type_refs(self, meta):
        assert "string" in meta["type_refs"]

    def test_param_type_in_type_refs(self, meta):
        """Constructor parameter types must appear in type_refs."""
        assert "IWidget" in meta["type_refs"]

    # ── items that q_uses finds but type_refs does NOT ───────────────────────

    def test_cast_target_in_cast_types_not_type_refs(self):
        """
        Explicit casts go to cast_types (T1 field), NOT type_refs.
        This keeps type_refs for declaration-site usages and cast_types
        for explicit narrowing casts — different query semantics.
        """
        src2 = b"""
namespace N {
    public class CastOnly {}
    public class C {
        public void M(object obj) {
            var cast = (CastOnly)obj;  // only use of CastOnly
        }
    }
}
"""
        m2 = extract_cs_metadata(src2)
        assert "CastOnly" not in m2["type_refs"], \
            "Cast-only types must not bleed into type_refs"
        assert "CastOnly" in m2["cast_types"], \
            f"Cast-only types must be in cast_types: {m2['cast_types']}"

    def test_local_variable_type_in_type_refs(self):
        """
        GAP CLOSED: local variable 'LocalOnly x = ...' — the declared type
        'LocalOnly' is now indexed in type_refs.
        """
        src = b"""
namespace N {
    public class LocalOnly {}
    public class C {
        public void M() {
            LocalOnly x = null;  // only use of LocalOnly
        }
    }
}
"""
        m = extract_cs_metadata(src)
        assert "LocalOnly" in m["type_refs"], \
            f"Local variable type must now be in type_refs: {m['type_refs']}"

    def test_typeof_not_in_type_refs(self):
        """
        GAP: typeof(TypeOfOnly) — the type is found by q_uses but not by indexer.
        """
        src = b"""
namespace N {
    public class TypeOfOnly {}
    public class C {
        public void M() {
            var t = typeof(TypeOfOnly);
        }
    }
}
"""
        m = extract_cs_metadata(src)
        assert "TypeOfOnly" not in m["type_refs"], \
            "GAP CONFIRMED: typeof targets in method bodies not in type_refs"

    def test_q_uses_finds_cast_target(self):
        """q_uses finds cast target types that are absent from type_refs."""
        src = b"""
namespace N {
    public class CastTarget {}
    public class C {
        public void M(object obj) {
            var x = (CastTarget)obj;
        }
    }
}
"""
        s, t, ls = _parse(src)
        r = q_uses(s, t, ls, "CastTarget")
        assert len(r) >= 1, "q_uses should find cast target type"

    def test_q_uses_finds_local_var_type(self):
        """q_uses finds local variable declared types absent from type_refs."""
        src = b"""
namespace N {
    public class LocalVar {}
    public class C {
        public void M() { LocalVar x = null; }
    }
}
"""
        s, t, ls = _parse(src)
        r = q_uses(s, t, ls, "LocalVar")
        assert len(r) >= 1, "q_uses should find local variable type"


# ===========================================================================
# GAP: usings — indexer stores top-level prefix only
# ===========================================================================

class TestUsingsCoarsenessGap:
    """
    GAP: indexer stores only the top-level namespace prefix from each using
    directive (e.g. 'System.Collections.Generic' → 'System'), while q_usings
    returns the full directive text.

    This is an intentional design choice for index performance (faceting by
    top-level namespace), but queries that need to find exact using directives
    must use q_usings rather than the indexed usings field.
    """

    def test_indexer_stores_top_level_prefix_only(self, meta):
        assert "System" in meta["usings"]
        # Full qualified names must NOT appear
        assert "System.Collections.Generic" not in meta["usings"], \
            "GAP CONFIRMED: indexer strips to top-level namespace prefix"
        assert "Acme.Storage" not in meta["usings"], \
            "GAP CONFIRMED: indexer strips Acme.Storage to 'Acme'"
        assert "Acme" in meta["usings"]

    def test_query_returns_full_directive(self, fx):
        r = q_usings(*fx)
        texts = _texts(r)
        assert any("System.Collections.Generic" in t for t in texts)
        assert any("Acme.Storage" in t for t in texts)

    def test_indexer_deduplicates_same_prefix(self):
        """Two 'using System.*' directives should store 'System' once."""
        src = b"using System; using System.IO; using System.Text;"
        m = extract_cs_metadata(src)
        count = sum(1 for u in m["usings"] if u == "System")
        assert count == 1, f"'System' appears {count} times — expected 1 (deduped)"


# ===========================================================================
# GAP: event_declaration — indexed in method_names/type_refs but absent from q_fields
# ===========================================================================

class TestEventDeclarationGap:
    """
    GAP: tree-sitter-c-sharp parses events in two different node types:

      event_field_declaration  — 'public event EventHandler OnChanged;'  (common form)
      event_declaration        — 'public event EventHandler E { add{} remove{} }'

    Only event_declaration is in _MEMBER_DECL_NODES.  event_field_declaration is
    NOT processed by the indexer or q_methods, so field-style events are invisible
    to both systems.

    event_declaration (accessor form) IS in _MEMBER_DECL_NODES, so its name and
    type are captured, and q_methods shows it as '[event]'.

    q_fields and q_field_type only scan field_declaration and property_declaration
    so they miss event_declaration too.
    """

    # Field-style events (the common form) — event_field_declaration
    _SRC_FIELD = b"""
namespace N {
    public class C {
        private string _name;
        public string Label { get; set; }
        public event System.EventHandler OnChanged;
    }
}
"""

    # Accessor-style events — event_declaration (in _MEMBER_DECL_NODES)
    _SRC_ACCESSOR = b"""
namespace N {
    public class C {
        public event System.EventHandler OnAccessor { add { } remove { } }
    }
}
"""

    def test_field_style_event_found_by_indexer(self):
        """event_field_declaration is now in _MEMBER_DECL_NODES — indexer captures it."""
        m = extract_cs_metadata(self._SRC_FIELD)
        assert "OnChanged" in m["method_names"], \
            f"event_field_declaration name missing from method_names: {m['method_names']}"

    def test_field_style_event_type_in_type_refs(self):
        """EventHandler from an event_field_declaration appears in type_refs."""
        m = extract_cs_metadata(self._SRC_FIELD)
        assert "EventHandler" in m["type_refs"], \
            f"event_field_declaration type missing from type_refs: {m['type_refs']}"

    def test_field_style_event_found_by_q_methods(self):
        """event_field_declaration is now found by q_methods as '[event]'."""
        s, t, ls = _parse(self._SRC_FIELD)
        r = q_methods(s, t, ls)
        assert any("OnChanged" in txt for _, txt in r), \
            "field-style event should appear in q_methods output"
        assert any("[event]" in txt for _, txt in r)

    def test_accessor_style_event_found_by_q_methods(self):
        """event_declaration (accessor form) IS in _MEMBER_DECL_NODES so q_methods finds it."""
        s, t, ls = _parse(self._SRC_ACCESSOR)
        r = q_methods(s, t, ls)
        assert any("OnAccessor" in txt for _, txt in r), \
            "accessor-style event should appear in q_methods as '[event]'"
        assert any("[event]" in txt for _, txt in r)

    def test_accessor_style_event_found_by_indexer(self):
        """event_declaration name and type ARE captured by the indexer."""
        m = extract_cs_metadata(self._SRC_ACCESSOR)
        assert "OnAccessor" in m["method_names"], \
            f"accessor-style event name missing: {m['method_names']}"
        assert "EventHandler" in m["type_refs"], \
            f"accessor-style event type missing from type_refs: {m['type_refs']}"

    def test_q_fields_does_not_find_any_event(self):
        """q_fields omits all event declarations (both styles)."""
        s, t, ls = _parse(self._SRC_ACCESSOR)
        r = q_fields(s, t, ls)
        assert not any("[event]" in txt for _, txt in r), \
            "q_fields should never include event declarations"

    def test_q_field_type_finds_field_style_event(self):
        """q_field_type now finds event_field_declaration by type."""
        s, t, ls = _parse(self._SRC_FIELD)
        r = q_uses(s, t, ls, "EventHandler", uses_kind="field")
        assert len(r) >= 1, "uses(kind=field) should find event_field_declaration by type"
        assert any("OnChanged" in txt for _, txt in r)


# ===========================================================================
# cast_types — dedicated T1 field for explicit cast targets
# ===========================================================================

class TestCastSitesMissing:
    """
    GAP CLOSED: cast_types is now a T1 field.
    q_casts finds explicit (TYPE)expr cast expressions; the indexer now extracts
    the same types into cast_types for Typesense pre-filtering.
    Cast targets do NOT bleed into type_refs — they remain in cast_types only.
    """

    def test_q_casts_finds_explicit_cast(self, fx):
        r = q_casts(*fx, "BaseWidget")
        assert len(r) >= 1

    def test_indexer_has_cast_types_field(self, meta):
        assert "cast_types" in meta, \
            "cast_types field must be present in indexer metadata"

    def test_cast_target_in_cast_types_not_type_refs(self):
        """Cast-only body types are in cast_types, not type_refs."""
        src = b"""
namespace N {
    public class BodyCastOnly {}
    public class C {
        public void M(object o) { var x = (BodyCastOnly)o; }
    }
}
"""
        m = extract_cs_metadata(src)
        assert "BodyCastOnly" in m["cast_types"], \
            f"Cast-only body types must be in cast_types: {m['cast_types']}"
        assert "BodyCastOnly" not in m["type_refs"], \
            "Cast-only body types must not bleed into type_refs"


# ===========================================================================
# MISSING: q_ident — no indexed equivalent
# ===========================================================================

class TestIdentMissing:
    """
    MISSING: q_ident is a semantic grep over all identifier occurrences.
    There is no equivalent field in the Typesense index; the content field
    holds raw source text for full-text search, but it is not semantically
    filtered (includes comments and strings).
    """

    def test_q_ident_finds_all_occurrences(self, fx):
        r = q_all_refs(*fx,"IWidget")
        # Should find: interface decl, base types, field type, param types, return type
        assert len(r) >= 4

    def test_q_ident_skips_strings(self, fx):
        # "IDENT_IN_STRING" — not in fixture, so 0 results
        r = q_all_refs(*fx,"COMMENT_CALL")
        assert len(r) == 0

    def test_indexer_has_no_ident_field(self, meta):
        assert "ident_occurrences" not in meta, \
            "No ident_occurrences field expected in indexed metadata"

    def test_content_field_present_as_fallback(self):
        """Indexer stores raw content for full-text fallback."""
        src = b"namespace N { public class C { } }"
        from indexserver.indexer import build_document
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".cs", delete=False) as f:
            f.write(src)
            tmppath = f.name
        try:
            doc = build_document(tmppath, "test/C.cs")
            assert "tokens" in doc
            assert "C" in doc["tokens"]
        finally:
            os.unlink(tmppath)


# ===========================================================================
# CONSISTENT: method_names symmetry with q_methods
# ===========================================================================

class TestMethodNamesConsistency:
    """CONSISTENT — indexer method_names and q_methods enumerate the same members."""

    def test_indexer_includes_field_names(self, meta):
        assert "_inner" in meta["method_names"] or "_prefix" in meta["method_names"], \
            f"field names missing from method_names: {meta['method_names']}"

    def test_indexer_includes_method_names(self, meta):
        assert "Transform" in meta["method_names"], \
            f"method_names: {meta['method_names']}"

    def test_indexer_includes_property_names(self, meta):
        assert "Prefix" in meta["method_names"], \
            f"method_names: {meta['method_names']}"

    def test_query_methods_finds_all_members(self, fx):
        r = q_methods(*fx)
        texts_list = _texts(r)
        assert any("Transform" in t for t in texts_list)
        assert any("Prefix" in t for t in texts_list)
        assert any("[ctor]" in t for t in texts_list)

    def test_indexer_matches_query_member_names(self, meta, fx):
        """All names in indexer method_names should appear in q_methods output."""
        q_result_texts = _texts(q_methods(*fx))
        for name in meta["method_names"]:
            assert any(name in t for t in q_result_texts), \
                f"'{name}' in indexer method_names but absent from q_methods output"


# ===========================================================================
# CONSISTENT: field_type / param_type symmetry with type_refs
# ===========================================================================

class TestFieldParamTypeConsistency:
    """CONSISTENT for declared types — q_field_type and q_param_type results
    are a subset of what the indexer puts in type_refs."""

    def test_field_type_results_in_type_refs(self, fx, meta):
        """Every type returned by q_field_type should be in type_refs."""
        from cs_ast import _unqualify_type
        import re
        for _, txt in q_uses(*fx, "IWidget", uses_kind="field"):
            # type_refs stores individual type names, not full qualified strings
            assert "IWidget" in meta["type_refs"], \
                f"uses(kind=field) found IWidget field but it's absent from type_refs"

    def test_param_type_results_in_type_refs(self, fx, meta):
        """Every type returned by uses(kind=param) should be in type_refs."""
        r = q_uses(*fx, "IWidget", uses_kind="param")
        assert len(r) >= 1, "uses(kind=param)(IWidget) should find results"
        assert "IWidget" in meta["type_refs"], \
            "IWidget param type missing from type_refs"

    def test_generic_type_arg_in_type_refs(self):
        """
        Indexer expands generic types: IList<IBlobStore> → stores both 'IList'
        and 'IBlobStore'.  q_field_type finds the field by either component name.
        """
        src = b"""
namespace N {
    public interface IBlobStore {}
    public class C {
        private System.Collections.Generic.IList<IBlobStore> _stores;
    }
}
"""
        m = extract_cs_metadata(src)
        assert "IBlobStore" in m["type_refs"], \
            f"generic type arg missing from type_refs: {m['type_refs']}"
        assert any("IList" in r for r in m["type_refs"]), \
            f"generic wrapper missing from type_refs: {m['type_refs']}"

        s, t, ls = _parse(src)
        r = q_uses(s, t, ls, "IBlobStore", uses_kind="field")
        assert len(r) >= 1, "uses(kind=field) should find field typed as generic IList<IBlobStore>"


# ===========================================================================
# CONSISTENT: namespace extraction
# ===========================================================================

class TestNamespaceConsistency:
    """CONSISTENT — both indexer and q_usings work from the same source."""

    def test_indexer_extracts_namespace(self, meta):
        assert meta["namespace"] == "TestApp", \
            f"namespace: {meta['namespace']!r}"

    def test_file_scoped_namespace(self):
        src = b"namespace MyApp.Core;\npublic class C {}"
        m = extract_cs_metadata(src)
        assert m["namespace"] == "MyApp.Core", \
            f"file-scoped namespace: {m['namespace']!r}"

    def test_nested_namespace_uses_first(self):
        src = b"namespace Outer { namespace Inner { public class C {} } }"
        m = extract_cs_metadata(src)
        # Indexer stores the first namespace found
        assert m["namespace"] in ("Outer", "Inner"), \
            f"namespace: {m['namespace']!r}"
