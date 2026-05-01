"""
Tests for C# query modes using sample/root1/Throttle.cs.

Replicates behaviors observed during live testing on the ExponentialBackoff
pattern from absblobstore (without any real SPO code):

  declarations, implements, calls, methods, classes
    → all work correctly

  accesses_of
    → correctly finds explicit dot-notation member accesses (expr.MEMBER)
    → does NOT find bare accesses where the name is used as a receiver

  accesses_on TYPE
    → tracks variables/fields declared as TYPE via variable_declaration nodes
      (field_declaration wraps a variable_declaration, so fields ARE tracked)
    → does NOT track properties declared as TYPE (property_declaration has
      no inner variable_declaration) — known gap, marked xfail

Run (no Typesense needed):
    pytest tests/test_cs_throttle.py -v
"""
from __future__ import annotations
from .conftest import SAMPLE_ROOT1

import os
import unittest

from tests.base import _parse
from ..cs import (
    q_declarations,
    q_implements,
    q_calls,
    q_accesses_of,
    q_accesses_on,
    q_methods,
    q_classes,
)

# ---------------------------------------------------------------------------
# Load the fixture file once at module level
# ---------------------------------------------------------------------------

_SAMPLE = os.path.join(SAMPLE_ROOT1, "Throttle.cs")

with open(_SAMPLE, encoding="utf-8") as _f:
    _SRC = _f.read()

_PARSED = _parse(_SRC)   # (src_bytes, tree, lines)


def _lines_of(results):
    """Return the set of 1-based line numbers from a result list."""
    return {ln for ln, _ in results}


def _texts(results):
    """Concatenate all result texts into one string for assertion scanning."""
    return " ".join(t for _, t in results)


# ===========================================================================
# declarations
# ===========================================================================

class TestDeclarations(unittest.TestCase):

    def _decl(self, name, **kw):
        return q_declarations(*_PARSED, name=name, **kw)

    def test_finds_class_by_name(self):
        r = self._decl("ExponentialRetry")
        assert r, "ExponentialRetry class must be found"
        assert any("ExponentialRetry" in t for _, t in r)

    def test_finds_interface_by_name(self):
        r = self._decl("IRetryPolicy")
        assert r, "IRetryPolicy interface must be found"

    def test_finds_method_in_multiple_classes(self):
        """RecordAttempt is declared in both ExponentialRetry and FixedRetry."""
        r = self._decl("RecordAttempt")
        assert len(r) >= 2, f"Expected ≥2 RecordAttempt declarations, got {r}"

    def test_symbol_kind_class_excludes_interface(self):
        r_class = self._decl("IRetryPolicy", symbol_kind="class")
        assert r_class == [], "interface must not match symbol_kind=class"

    def test_nonexistent_returns_empty(self):
        assert self._decl("NoSuchType") == []


# ===========================================================================
# implements
# ===========================================================================

class TestImplements(unittest.TestCase):

    def _impl(self, name):
        return q_implements(*_PARSED, type_name=name)

    def test_finds_both_implementors(self):
        """ExponentialRetry and FixedRetry both implement IRetryPolicy."""
        r = self._impl("IRetryPolicy")
        assert len(r) == 2, f"Expected 2 implementors of IRetryPolicy, got {r}"

    def test_implementor_names_correct(self):
        r = self._impl("IRetryPolicy")
        text = _texts(r)
        assert "ExponentialRetry" in text
        assert "FixedRetry"       in text

    def test_retryrunner_not_returned(self):
        """RetryRunner does not implement IRetryPolicy."""
        r = self._impl("IRetryPolicy")
        assert not any("RetryRunner" in t for _, t in r)

    def test_nonexistent_interface_returns_empty(self):
        assert self._impl("INonExistent") == []


# ===========================================================================
# calls
# ===========================================================================

class TestCalls(unittest.TestCase):

    def _calls(self, name):
        return q_calls(*_PARSED, method_name=name)

    def test_finds_math_min(self):
        r = self._calls("Math.Min")
        assert r, "Math.Min call must be found"
        assert all("Min" in t for _, t in r)

    def test_finds_math_max(self):
        r = self._calls("Math.Max")
        assert r, "Math.Max call must be found"
        assert all("Max" in t for _, t in r)

    def test_math_min_and_max_on_different_lines(self):
        """Math.Min and Math.Max appear on separate lines."""
        min_lines = _lines_of(self._calls("Math.Min"))
        max_lines = _lines_of(self._calls("Math.Max"))
        assert min_lines.isdisjoint(max_lines), \
            f"Math.Min and Math.Max must be on distinct lines: {min_lines} vs {max_lines}"

    def test_finds_record_attempt_call(self):
        r = self._calls("RecordAttempt")
        assert r, "_policy.RecordAttempt(ok) call must be found"
        assert any("RecordAttempt" in t for _, t in r)

    def test_interface_method_not_a_call(self):
        """RecordAttempt declarations are not calls — only the call site in RetryRunner."""
        r = self._calls("RecordAttempt")
        assert len(r) == 1, \
            f"Expected exactly 1 call site for RecordAttempt, got {r}"

    def test_nonexistent_method_returns_empty(self):
        assert self._calls("NoSuchMethod") == []


# ===========================================================================
# accesses_of
# ===========================================================================

class TestAccessesOf(unittest.TestCase):
    """
    accesses_of MEMBER finds every member_access_expression whose *name* field
    equals MEMBER — i.e. patterns like `expr.MEMBER`.

    It does NOT find bare uses of MEMBER where it is itself the receiver
    (e.g. `Interval.TotalMilliseconds` is NOT an access OF Interval).
    """

    def _of(self, member):
        return q_accesses_of(*_PARSED, member_name=member)

    def test_finds_all_total_milliseconds_accesses(self):
        """TotalMilliseconds appears on the property and both fields."""
        r = self._of("TotalMilliseconds")
        # Line with Interval.TotalMilliseconds (if-branch) + two lines with
        # field.TotalMilliseconds and Interval.TotalMilliseconds each
        assert len(r) >= 3, \
            f"Expected ≥3 .TotalMilliseconds accesses, got {len(r)}: {r}"

    def test_total_milliseconds_found_in_if_condition(self):
        """Interval.TotalMilliseconds inside a simple `if` must be found."""
        r = self._of("TotalMilliseconds")
        texts = _texts(r)
        assert "Interval.TotalMilliseconds == 0" in texts or \
               "TotalMilliseconds" in texts, \
            "TotalMilliseconds in the if-condition must be found"

    def test_finds_interval_via_policy_field(self):
        """_policy.Interval in RetryRunner.Execute must be found."""
        r = self._of("Interval")
        assert r, "_policy.Interval must be found as an accesses_of result"
        assert any("_policy.Interval" in t for _, t in r)

    def test_interval_as_receiver_not_returned(self):
        """
        `Interval.TotalMilliseconds` — here Interval is the *receiver*, not
        the accessed member.  accesses_of "Interval" must NOT return these.
        """
        r = self._of("Interval")
        texts = _texts(r)
        assert "Interval.TotalMilliseconds" not in texts, \
            "Interval used as receiver must not appear in accesses_of 'Interval'"

    def test_finds_record_attempt_call_site(self):
        """_policy.RecordAttempt(ok) — RecordAttempt is the accessed member."""
        r = self._of("RecordAttempt")
        assert r, "_policy.RecordAttempt must be found"

    def test_different_member_not_returned(self):
        r = self._of("TotalMilliseconds")
        texts = _texts(r)
        assert "RecordAttempt" not in texts

    def test_nonexistent_member_returns_empty(self):
        assert self._of("NoSuchMember") == []


# ===========================================================================
# accesses_on
# ===========================================================================

class TestAccessesOn(unittest.TestCase):
    """
    accesses_on TYPE finds member accesses on variables *declared as* TYPE.

    Implementation walks variable_declaration nodes (captures local variables,
    parameters, and fields — because field_declaration wraps variable_declaration)
    plus var-inferred locals from new/as/cast expressions.

    Known gap: property_declaration nodes are not walked, so a property
    typed as TYPE is not added to the tracked variable set.  Any member
    access on that property is silently missed.
    """

    def _on(self, type_name):
        return q_accesses_on(*_PARSED, type_name=type_name)

    # --- Working cases -------------------------------------------------------

    def test_finds_accesses_on_timespans_fields(self):
        """
        _maxInterval and _minInterval are fields (variable_declaration inside
        field_declaration) typed as TimeSpan — their .TotalMilliseconds
        accesses must be found.
        """
        r = self._on("TimeSpan")
        assert r, "Expected TimeSpan member accesses via fields"
        texts = _texts(r)
        assert "TotalMilliseconds" in texts

    def test_finds_both_field_lines(self):
        """
        _minInterval.TotalMilliseconds (Math.Max line) and
        _maxInterval.TotalMilliseconds (Math.Min line) are on separate lines.
        """
        r = self._on("TimeSpan")
        assert len(r) >= 2, \
            f"Expected accesses on at least 2 distinct lines, got {r}"

    def test_finds_accesses_on_iretrypolicy_field(self):
        """
        _policy is a field typed as IRetryPolicy.
        RetryRunner.Execute calls _policy.RecordAttempt and reads _policy.Interval —
        both must be found.
        """
        r = self._on("IRetryPolicy")
        assert r, "Expected accesses on _policy (IRetryPolicy field)"
        texts = _texts(r)
        assert "RecordAttempt" in texts, f"RecordAttempt missing: {r}"
        assert "Interval"      in texts, f"Interval missing: {r}"

    def test_accesses_on_iretrypolicy_are_on_separate_lines(self):
        r = self._on("IRetryPolicy")
        assert len(r) >= 2, \
            f"Expected RecordAttempt and Interval on separate lines, got {r}"

    def test_unrelated_type_returns_empty(self):
        assert self._on("NoSuchType") == []

    # --- Known gap: properties are not tracked -------------------------------

    def test_finds_interval_property_access_in_if_condition(self):
        """
        Interval.TotalMilliseconds on the `if (Interval.TotalMilliseconds == 0)`
        line must be found when searching accesses_on "TimeSpan".
        Interval is a TimeSpan property — tracked via property_declaration.
        """
        r = self._on("TimeSpan")
        lines_found = _lines_of(r)
        # Locate the line number that contains the bare if-condition
        src_lines = _SRC.splitlines()
        if_line = next(
            (i + 1 for i, ln in enumerate(src_lines)
             if "Interval.TotalMilliseconds == 0" in ln),
            None,
        )
        assert if_line is not None, "Throttle.cs must contain the if-condition line"
        assert if_line in lines_found, (
            f"Line {if_line} (Interval.TotalMilliseconds == 0) not in results: {r}"
        )

    def test_timespans_access_count_includes_property_lines(self):
        """
        There are 3 lines with TimeSpan member accesses:
          - if (Interval.TotalMilliseconds == 0)                    ← property only
          - Math.Max(_minInterval.TotalMilliseconds, Interval…)     ← field + property
          - Math.Min(_maxInterval.TotalMilliseconds, Interval…)     ← field + property
        All 3 must be returned now that property_declaration is tracked.
        """
        r = self._on("TimeSpan")
        assert len(r) == 3, \
            f"Expected 3 TimeSpan-access lines (got {len(r)}): {r}"


# ===========================================================================
# methods / classes listing
# ===========================================================================

class TestListingModes(unittest.TestCase):

    def test_classes_finds_all_types(self):
        r = q_classes(*_PARSED)
        texts = _texts(r)
        assert "ExponentialRetry" in texts
        assert "FixedRetry"       in texts
        assert "RetryRunner"      in texts
        assert "IRetryPolicy"     in texts

    def test_classes_count(self):
        r = q_classes(*_PARSED)
        assert len(r) == 4, f"Expected 4 type declarations, got {r}"

    def test_methods_finds_record_attempt_in_both_classes(self):
        """RecordAttempt is declared in ExponentialRetry and FixedRetry."""
        r = q_methods(*_PARSED)
        record_hits = [t for _, t in r if "RecordAttempt" in t]
        assert len(record_hits) >= 2, \
            f"Expected RecordAttempt in ≥2 members, got {record_hits}"

    def test_methods_includes_fields_and_props(self):
        """methods listing mode returns all members: fields, props, ctors, methods."""
        r = q_methods(*_PARSED)
        kinds = {t.split("]")[0].lstrip("[") for _, t in r if "]" in t}
        assert "field"  in kinds, f"No field entries in methods listing: {kinds}"
        assert "prop"   in kinds, f"No prop entries in methods listing: {kinds}"
        assert "method" in kinds, f"No method entries in methods listing: {kinds}"
        assert "ctor"   in kinds, f"No ctor entries in methods listing: {kinds}"

    def test_methods_finds_execute(self):
        r = q_methods(*_PARSED)
        assert any("Execute" in t for _, t in r)

    def test_methods_finds_interval_property(self):
        r = q_methods(*_PARSED)
        assert any("Interval" in t for _, t in r)


if __name__ == "__main__":
    unittest.main()
