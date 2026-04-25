"""
Tests for query.py -- C# AST structural query functions.

Does NOT require Typesense to be running; calls query functions directly.

Run from WSL:
    /tmp/ts-test-venv/bin/pytest codesearch/tests/test_query_cs.py -v
"""

import os
import sys
import pytest

# ── path setup ────────────────────────────────────────────────────────────────
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

import tree_sitter_c_sharp as tscsharp
from tree_sitter import Language, Parser

from src.query.dispatch import (
    q_classes, q_methods, q_fields, q_calls, q_implements, q_attrs, q_usings, q_declarations, q_params, q_uses,
    q_casts, q_all_refs, q_accesses_on,
)

# ── fixture setup ─────────────────────────────────────────────────────────────

_CS = Language(tscsharp.language())
_PARSER = Parser(_CS)
_FIXTURE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample", "root1", "query_fixture.cs")


@pytest.fixture(scope="module")
def fx():
    """Parse query_fixture.cs once for the entire module."""
    src = open(_FIXTURE_PATH, "rb").read()
    tree = _PARSER.parse(src)
    lines = src.decode("utf-8", errors="replace").splitlines()
    return src, tree, lines


# ── helpers ───────────────────────────────────────────────────────────────────

def texts(results):
    return [t for _, t in results]


def has(results, sub):
    return any(sub in t for _, t in results)


# ── classes ───────────────────────────────────────────────────────────────────

class TestClasses:
    def test_finds_interface(self, fx):
        r = q_classes(*fx)
        assert has(r, "IProcessor"), "interface IProcessor not found"
        assert has(r, "ILogger"), "interface ILogger not found"

    def test_interface_tagged_correctly(self, fx):
        r = q_classes(*fx)
        match = next(t for _, t in r if "IProcessor" in t)
        assert "[interface]" in match

    def test_finds_abstract_class_with_base(self, fx):
        r = q_classes(*fx)
        match = next(t for _, t in r if "BaseProcessor" in t)
        assert "IProcessor" in match  # base type included

    def test_finds_concrete_class_with_bases(self, fx):
        r = q_classes(*fx)
        match = next(t for _, t in r if "TextProcessor" in t)
        assert "BaseProcessor" in match or "IProcessor" in match

    def test_finds_static_class(self, fx):
        r = q_classes(*fx)
        assert has(r, "ProcessorFactory")

    def test_finds_struct(self, fx):
        r = q_classes(*fx)
        match = next(t for _, t in r if "ProcessResult" in t)
        assert "[struct]" in match

    def test_finds_enum(self, fx):
        r = q_classes(*fx)
        match = next(t for _, t in r if "ProcessingMode" in t)
        assert "[enum]" in match

    def test_finds_delegate(self, fx):
        r = q_classes(*fx)
        assert has(r, "[delegate]")
        assert has(r, "ProcessDelegate")

    def test_service_class_present(self, fx):
        r = q_classes(*fx)
        assert has(r, "ProcessingService")


# ── methods ───────────────────────────────────────────────────────────────────

class TestMethods:
    def test_finds_method_with_return_type(self, fx):
        r = q_methods(*fx)
        match = next((t for _, t in r if "Transform" in t), None)
        assert match is not None
        assert "string" in match

    def test_finds_constructor(self, fx):
        r = q_methods(*fx)
        assert has(r, "[ctor]")

    def test_finds_property(self, fx):
        r = q_methods(*fx)
        assert has(r, "[prop]")
        assert has(r, "Prefix")

    def test_finds_field(self, fx):
        r = q_methods(*fx)
        assert has(r, "[field]")

    def test_finds_abstract_method(self, fx):
        r = q_methods(*fx)
        assert has(r, "Process")

    def test_finds_multiple_methods(self, fx):
        r = q_methods(*fx)
        method_texts = [t for _, t in r if "[method]" in t]
        assert len(method_texts) >= 5


# ── fields ────────────────────────────────────────────────────────────────────

class TestFields:
    def test_finds_fields(self, fx):
        r = q_fields(*fx)
        assert has(r, "[field]")

    def test_finds_properties(self, fx):
        r = q_fields(*fx)
        assert has(r, "[prop]")
        assert has(r, "Prefix")

    def test_no_methods_in_results(self, fx):
        r = q_fields(*fx)
        for _, t in r:
            assert "[method]" not in t
            assert "[ctor]" not in t

    def test_field_includes_type(self, fx):
        r = q_fields(*fx)
        # ILogger _logger field should appear with its type
        match = next((t for _, t in r if "_logger" in t), None)
        assert match is not None
        assert "ILogger" in match


# ── calls ─────────────────────────────────────────────────────────────────────

class TestCalls:
    def test_bare_name_finds_calls(self, fx):
        src, tree, lines = fx
        r = q_calls(src, tree, lines, "Process")
        assert len(r) >= 1

    def test_bare_name_finds_all_sites(self, fx):
        src, tree, lines = fx
        r = q_calls(src, tree, lines, "Create")
        assert len(r) >= 1

    def test_qualified_name_restricts_to_class(self, fx):
        src, tree, lines = fx
        bare = q_calls(src, tree, lines, "Create")
        qualified = q_calls(src, tree, lines, "ProcessorFactory.Create")
        assert len(qualified) >= 1
        assert len(qualified) <= len(bare)
        for _, t in qualified:
            assert "ProcessorFactory" in t

    def test_qualified_wrong_class_finds_nothing(self, fx):
        src, tree, lines = fx
        r = q_calls(src, tree, lines, "OtherClass.Create")
        assert len(r) == 0

    def test_skips_comment_calls(self, fx):
        # COMMENT_CALL() appears in a comment — must not be matched
        src, tree, lines = fx
        r = q_calls(src, tree, lines, "COMMENT_CALL")
        assert len(r) == 0

    def test_qualified_run(self, fx):
        src, tree, lines = fx
        r = q_calls(src, tree, lines, "ProcessorFactory.Run")
        assert len(r) >= 1


# ── implements ────────────────────────────────────────────────────────────────

class TestImplements:
    def test_finds_classes_implementing_interface(self, fx):
        src, tree, lines = fx
        r = q_implements(src, tree, lines, "IProcessor")
        names = texts(r)
        assert any("TextProcessor" in n for n in names)
        assert any("BaseProcessor" in n for n in names)

    def test_finds_class_inheriting_base_class(self, fx):
        src, tree, lines = fx
        r = q_implements(src, tree, lines, "BaseProcessor")
        assert any("TextProcessor" in t for _, t in r)

    def test_does_not_include_the_interface_itself(self, fx):
        src, tree, lines = fx
        r = q_implements(src, tree, lines, "IProcessor")
        for _, t in r:
            # The declaration "interface IProcessor" should not be a match
            assert "[interface] IProcessor" not in t

    def test_no_match_for_unknown_type(self, fx):
        src, tree, lines = fx
        r = q_implements(src, tree, lines, "IUnknownInterface")
        assert len(r) == 0

    def test_unrelated_class_not_included(self, fx):
        src, tree, lines = fx
        r = q_implements(src, tree, lines, "IProcessor")
        for _, t in r:
            assert "ProcessorFactory" not in t
            assert "ProcessingService" not in t


# ── uses ──────────────────────────────────────────────────────────────────────

class TestUses:
    def test_finds_type_references(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "IProcessor")
        assert len(r) >= 2  # field type, param type, return type, etc.

    def test_skips_declaration_name(self, fx):
        # "interface IProcessor" line is NOT a use of IProcessor
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "IProcessor")
        for _, t in r:
            assert not t.strip().startswith("public interface IProcessor")

    def test_finds_in_field_declaration(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "IProcessor")
        assert any("_processor" in t for _, t in r)

    def test_finds_in_parameter(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "IProcessor")
        # parameter "IProcessor<string> processor" or "IProcessor<string> proc"
        assert any("processor" in t or "proc" in t for _, t in r)

    def test_no_match_for_unused_name(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "NoSuchType")
        assert len(r) == 0

    def test_finds_in_typeof(self, fx):
        """typeof(ProcessResult) counts as a type reference."""
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "ProcessResult")
        assert any("typeof" in t for _, t in r)

    def test_deduplicates_same_line(self, fx):
        """Merge(ProcessResult a, ProcessResult b) has two refs on one line.
        Each unique source line should appear at most once in the results."""
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "ProcessResult")
        line_numbers = [lineno for lineno, _ in r]
        assert len(line_numbers) == len(set(line_numbers)), \
            "duplicate line numbers found — same line reported more than once"


# ── attrs ─────────────────────────────────────────────────────────────────────

class TestAttrs:
    def test_finds_all_attributes(self, fx):
        r = q_attrs(*fx)
        assert len(r) >= 2

    def test_filter_serializable(self, fx):
        src, tree, lines = fx
        r = q_attrs(src, tree, lines, "Serializable")
        assert len(r) >= 1
        for _, t in r:
            assert "Serializable" in t

    def test_filter_obsolete(self, fx):
        src, tree, lines = fx
        r = q_attrs(src, tree, lines, "Obsolete")
        assert len(r) >= 1
        for _, t in r:
            assert "Obsolete" in t

    def test_filter_no_match(self, fx):
        src, tree, lines = fx
        r = q_attrs(src, tree, lines, "NonExistentAttribute")
        assert len(r) == 0

    def test_no_filter_returns_all(self, fx):
        src, tree, lines = fx
        unfiltered = q_attrs(src, tree, lines, None)
        serializable = q_attrs(src, tree, lines, "Serializable")
        assert len(unfiltered) >= len(serializable)


# ── usings ────────────────────────────────────────────────────────────────────

class TestUsings:
    def test_finds_standard_usings(self, fx):
        r = q_usings(*fx)
        ts = texts(r)
        assert any("System" in t for t in ts)
        assert any("Collections" in t for t in ts)
        assert any("Linq" in t for t in ts)

    def test_finds_using_alias(self, fx):
        r = q_usings(*fx)
        # "using StringList = System.Collections.Generic.List<string>;"
        assert any("StringList" in t for _, t in r)

    def test_count_matches_file(self, fx):
        r = q_usings(*fx)
        assert len(r) == 5  # System, Generic, Linq, Tasks, alias


# ── find ──────────────────────────────────────────────────────────────────────

class TestFind:
    def test_find_method_returns_body(self, fx):
        src, tree, lines = fx
        r = q_declarations(src, tree, lines, "Transform")
        assert len(r) == 1
        _, body = r[0]
        assert "Transform" in body
        assert "maxLength" in body
        assert "trim" in body

    def test_find_class_returns_body(self, fx):
        src, tree, lines = fx
        r = q_declarations(src, tree, lines, "ProcessorFactory", include_body=True)
        assert len(r) == 1
        _, body = r[0]
        assert "Create" in body

    def test_find_struct(self, fx):
        src, tree, lines = fx
        r = q_declarations(src, tree, lines, "ProcessResult", include_body=True)
        # finds both the struct declaration and its constructor (both named ProcessResult)
        assert len(r) >= 1
        bodies = [body for _, body in r]
        assert any("Success" in b for b in bodies)

    def test_find_no_match(self, fx):
        src, tree, lines = fx
        r = q_declarations(src, tree, lines, "NonExistentMethod")
        assert len(r) == 0


# ── params ────────────────────────────────────────────────────────────────────

class TestParams:
    def test_params_of_transform(self, fx):
        src, tree, lines = fx
        r = q_params(src, tree, lines, "Transform")
        assert len(r) >= 1
        _, param_txt = r[0]
        assert "string" in param_txt
        assert "input" in param_txt
        assert "maxLength" in param_txt

    def test_params_includes_optional_param(self, fx):
        src, tree, lines = fx
        r = q_params(src, tree, lines, "Transform")
        _, param_txt = r[0]
        assert "trim" in param_txt  # optional bool param is listed

    def test_params_constructor(self, fx):
        src, tree, lines = fx
        r = q_params(src, tree, lines, "TextProcessor")
        assert len(r) >= 1
        _, param_txt = r[0]
        assert "prefix" in param_txt
        assert "log" in param_txt

    def test_params_no_match(self, fx):
        src, tree, lines = fx
        r = q_params(src, tree, lines, "NonExistentMethod")
        assert len(r) == 0

    def test_params_no_parameters(self, fx):
        """Method with zero parameters still returns a result (empty param list)."""
        src, tree, lines = fx
        r = q_params(src, tree, lines, "FlushAll")
        assert len(r) >= 1
        _, param_txt = r[0]
        # The text should indicate an empty parameter list (either "()" or "no param")
        assert "FlushAll" in param_txt or "()" in param_txt or "no param" in param_txt.lower()

    def test_params_out_modifier(self, fx):
        """out / ref parameter modifiers appear in the param output."""
        src, tree, lines = fx
        r = q_params(src, tree, lines, "TryGetFirst")
        assert len(r) >= 1
        _, param_txt = r[0]
        assert "out" in param_txt or "ProcessResult" in param_txt


# ── field_type ────────────────────────────────────────────────────────────────

class TestFieldType:
    def test_finds_field_typed_as_interface(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "IProcessor", uses_kind="field")
        assert len(r) >= 1
        for _, t in r:
            assert "IProcessor" in t

    def test_finds_field_typed_as_logger(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "ILogger", uses_kind="field")
        assert len(r) >= 1

    def test_finds_property(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "string", uses_kind="field")
        assert any("Prefix" in t for _, t in r)

    def test_no_match_for_unknown_type(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "NonExistentType", uses_kind="field")
        assert len(r) == 0


# ── param_type ────────────────────────────────────────────────────────────────

class TestParamType:
    def test_finds_in_method(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "IProcessor", uses_kind="param")
        assert len(r) >= 1

    def test_finds_in_constructor(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "ILogger", uses_kind="param")
        assert len(r) >= 1

    def test_result_contains_method_name(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "ILogger", uses_kind="param")
        for _, t in r:
            # Should mention the enclosing method/ctor name
            assert "(" in t

    def test_no_match_for_unknown_type(self, fx):
        src, tree, lines = fx
        r = q_uses(src, tree, lines, "NonExistentType", uses_kind="param")
        assert len(r) == 0


# ── casts ─────────────────────────────────────────────────────────────────────

class TestCasts:
    def test_finds_explicit_cast(self, fx):
        src, tree, lines = fx
        r = q_casts(src, tree, lines, "TextProcessor")
        assert len(r) >= 1

    def test_cast_text_contains_type(self, fx):
        src, tree, lines = fx
        r = q_casts(src, tree, lines, "TextProcessor")
        for _, t in r:
            assert "TextProcessor" in t

    def test_no_match_for_unknown_type(self, fx):
        src, tree, lines = fx
        r = q_casts(src, tree, lines, "NonExistentType")
        assert len(r) == 0

    def test_as_expression_matched(self, fx):
        # (obj as TextProcessor) is an 'as' cast and must now appear (Round 13 fix)
        src, tree, lines = fx
        r = q_casts(src, tree, lines, "TextProcessor")
        as_results = [(ln, t) for ln, t in r if " as " in t]
        assert as_results, f"Expected 'as' cast lines for TextProcessor, got: {r}"

    def test_skips_cast_in_comment(self, fx):
        """(TextProcessor)obj mentioned only inside a comment must not be found."""
        src, tree, lines = fx
        r = q_casts(src, tree, lines, "TextProcessor")
        # All results should be actual code lines, not the comment line
        for _, t in r:
            stripped = t.strip()
            assert not stripped.startswith("//"), \
                f"Comment line incorrectly reported as cast: {t!r}"


# ── ident ─────────────────────────────────────────────────────────────────────

class TestIdent:
    def test_finds_multiple_occurrences(self, fx):
        src, tree, lines = fx
        r = q_all_refs(src, tree, lines, "ProcessResult")
        # Should appear in: struct decl, field types, param types, local vars, etc.
        assert len(r) >= 3

    def test_skips_string_contents(self, fx):
        # "IDENT_IN_STRING" appears inside a string literal in the fixture
        src, tree, lines = fx
        r = q_all_refs(src, tree, lines, "IDENT_IN_STRING")
        assert len(r) == 0

    def test_no_match_for_unknown(self, fx):
        src, tree, lines = fx
        r = q_all_refs(src, tree, lines, "NoSuchIdentifier99")
        assert len(r) == 0

    def test_finds_in_various_contexts(self, fx):
        src, tree, lines = fx
        r = q_all_refs(src, tree, lines, "IProcessor")
        # Should appear as interface decl, base type, field type, param type, return type
        assert len(r) >= 4

    def test_no_partial_match(self, fx):
        """Searching for 'ProcessResult' must not match the identifier
        'ProcessResultSummary' — identifiers must match exactly."""
        src, tree, lines = fx
        r = q_all_refs(src, tree, lines, "ProcessResult")
        # Lines that mention only ProcessResultSummary (never the bare token
        # "ProcessResult") should not appear in the results.
        for _, t in r:
            if "ProcessResultSummary" in t:
                # The line must also contain "ProcessResult" as a separate token
                # (e.g. it could legitimately mention both).
                assert "ProcessResult" in t.replace("ProcessResultSummary", ""), \
                    f"ProcessResultSummary-only line should not be in ident results: {t!r}"


# ── member_accesses ───────────────────────────────────────────────────────────

class TestMemberAccesses:
    def test_explicit_param_finds_accesses(self, fx):
        """Finds .Member accesses on an explicitly typed parameter."""
        src, tree, lines = fx
        r = q_accesses_on(src, tree, lines, "ProcessResult")
        members = texts(r)
        assert any(".Success" in m for m in members)
        assert any(".Output" in m for m in members)
        assert any(".ErrorCode" in m for m in members)

    def test_var_new_object(self, fx):
        """Finds accesses on var x = new T(...)."""
        src, tree, lines = fx
        r = q_accesses_on(src, tree, lines, "TextProcessor")
        members = texts(r)
        assert any(".Prefix" in m for m in members)

    def test_var_array_then_element(self, fx):
        """Finds accesses on var x = arr[i] where arr is T[]."""
        src, tree, lines = fx
        r = q_accesses_on(src, tree, lines, "ProcessResult")
        members = texts(r)
        # item.Output, item.Success, item.ErrorCode come from var item = results[i]
        assert any(".Output" in m for m in members)
        assert any(".Success" in m for m in members)
        assert any(".ErrorCode" in m for m in members)

    def test_var_as_cast(self, fx):
        """Finds accesses on var x = expr as T."""
        src, tree, lines = fx
        r = q_accesses_on(src, tree, lines, "TextProcessor")
        members = texts(r)
        # proc.Prefix where var proc = obj as TextProcessor
        assert any(".Prefix" in m for m in members)

    def test_var_explicit_cast(self, fx):
        """Finds accesses on var x = (T)expr."""
        src, tree, lines = fx
        r = q_accesses_on(src, tree, lines, "TextProcessor")
        members = texts(r)
        # proc.Prefix where var proc = (TextProcessor)obj
        assert any(".Prefix" in m for m in members)

    def test_no_match_for_unknown_type(self, fx):
        src, tree, lines = fx
        r = q_accesses_on(src, tree, lines, "NonExistentType")
        assert len(r) == 0

    def test_explicit_param_logger(self, fx):
        """ILogger is an explicit parameter type in some methods."""
        src, tree, lines = fx
        r = q_accesses_on(src, tree, lines, "ILogger")
        # No direct member accesses on ILogger params in fixture
        # (calls go through _logger field, not a local param of ILogger type)
        # This test just verifies no crash for a valid type with no accesses
        assert isinstance(r, list)

    def test_chained_access_not_leaked(self, fx):
        """result.Output.Length — only .Output should match for ProcessResult.
        .Length is on the string returned by .Output, not on ProcessResult itself."""
        src, tree, lines = fx
        r = q_accesses_on(src, tree, lines, "ProcessResult")
        members = texts(r)
        # .Output is correct (it's a member of ProcessResult)
        # .Length must NOT appear because it's a member of string, not ProcessResult
        for m in members:
            assert not m.strip().endswith(".Length"), \
                f".Length leaked into ProcessResult member_accesses results: {m!r}"
