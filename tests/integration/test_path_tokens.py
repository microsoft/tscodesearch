"""
End-to-end tests for path_tokens and namespace component indexing.

Verifies the new schema's domain-appropriate splitting:
  * filenames split into [full, stem, extension]
  * every directory name in the relative path is its own raw token, so a
    search for "billing" finds files under services/billing/ at any depth
  * namespaces split on `.` (C#/Python/Java/TypeScript style), so a search
    for "Billing" finds files in the Acme.Billing.Service namespace
  * raw tokenizer is case-sensitive and never splits on underscore — so
    ``add_text_field`` is one token, not three
"""
from __future__ import annotations

import os
import sys
import shutil
import tempfile
import unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

from tests.helpers import make_test_backend
from indexserver.indexer import (
    build_document, path_tokens_from_path, _split_namespace, _split_filename,
)
from indexserver.search import search as _search


# ── path_tokens_from_path (pure helper) ───────────────────────────────────────

class TestPathTokensFromPath(unittest.TestCase):

    def test_single_level_file(self):
        self.assertEqual(path_tokens_from_path("Foo.cs"),
                         ["Foo.cs", "Foo", "cs"])

    def test_two_level_file(self):
        self.assertEqual(path_tokens_from_path("services/Foo.cs"),
                         ["services", "Foo.cs", "Foo", "cs"])

    def test_deep_path_each_directory_is_a_token(self):
        out = path_tokens_from_path("services/billing/legacy/Foo.cs")
        # Each ancestor directory name is its own token — order is shallow-first.
        self.assertEqual(out[:3], ["services", "billing", "legacy"])
        # Followed by filename components.
        self.assertIn("Foo.cs", out)
        self.assertIn("Foo", out)
        self.assertIn("cs", out)

    def test_filename_multi_dot(self):
        # Widget.Test.cs → keep the full basename + every dot-separated piece.
        out = path_tokens_from_path("Widget.Test.cs")
        self.assertIn("Widget.Test.cs", out)
        self.assertIn("Widget", out)
        self.assertIn("Test", out)
        self.assertIn("cs", out)

    def test_normalises_backslashes(self):
        self.assertEqual(path_tokens_from_path("services\\billing\\Foo.cs"),
                         ["services", "billing", "Foo.cs", "Foo", "cs"])

    def test_empty_string(self):
        self.assertEqual(path_tokens_from_path(""), [])

    def test_no_duplicates(self):
        # services/services/Foo.cs would otherwise produce two "services" tokens.
        out = path_tokens_from_path("services/services/Foo.cs")
        self.assertEqual(out.count("services"), 1)


# ── _split_namespace ──────────────────────────────────────────────────────────

class TestSplitNamespace(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(_split_namespace(""), [])
        self.assertEqual(_split_namespace(None), [])

    def test_single_component(self):
        self.assertEqual(_split_namespace("TestApp"), ["TestApp"])

    def test_dotted(self):
        self.assertEqual(_split_namespace("Acme.Billing.Service"),
                         ["Acme", "Billing", "Service"])

    def test_accepts_pre_split_list(self):
        # Languages whose namespace separator isn't ``.`` can return a list
        # directly from their extractor.
        self.assertEqual(_split_namespace(["std", "collections", "HashMap"]),
                         ["std", "collections", "HashMap"])


# ── _split_filename ───────────────────────────────────────────────────────────

class TestSplitFilename(unittest.TestCase):

    def test_simple(self):
        self.assertEqual(_split_filename("Foo.cs"), ["Foo.cs", "Foo", "cs"])

    def test_no_extension(self):
        self.assertEqual(_split_filename("Makefile"), ["Makefile"])

    def test_multi_dot(self):
        self.assertEqual(_split_filename("Widget.Test.cs"),
                         ["Widget.Test.cs", "Widget", "Test", "cs"])


# ── End-to-end: real Tantivy backend, subpath search ──────────────────────────

class TestSubpathSearchAgainstRealIndex(unittest.TestCase):
    """A query for any directory name in the path returns every file under
    that directory, at any depth."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="ts_path_tokens_test_")
        self.backend, self._cleanup = make_test_backend()

        # Three files at different depths, under three different ancestor
        # directories. Same class name so we can disambiguate purely by path.
        self.files = {
            "services/billing/Invoice.cs":          "namespace Sample { public class Invoice {} }",
            "services/orders/legacy/Invoice.cs":    "namespace Sample { public class Invoice {} }",
            "tests/billing/InvoiceTests.cs":        "namespace Sample { public class InvoiceTests {} }",
        }
        for rel, body in self.files.items():
            full = os.path.join(self.workdir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(body)
            doc = build_document(full, rel)
            self.backend.upsert_many([doc])

    def tearDown(self):
        self._cleanup()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _rels(self, q: str) -> set[str]:
        result = _search(self.backend, q=q,
                         query_by="path_tokens", per_page=20)
        return {h["document"]["relative_path"] for h in result.get("hits", [])}

    def test_billing_matches_all_files_under_any_billing_dir(self):
        # `billing` appears as an intermediate directory in two files.
        rels = self._rels("billing")
        self.assertEqual(rels, {
            "services/billing/Invoice.cs",
            "tests/billing/InvoiceTests.cs",
        })

    def test_legacy_matches_only_the_deeply_nested_file(self):
        self.assertEqual(self._rels("legacy"),
                         {"services/orders/legacy/Invoice.cs"})

    def test_services_matches_files_under_services(self):
        self.assertEqual(self._rels("services"), {
            "services/billing/Invoice.cs",
            "services/orders/legacy/Invoice.cs",
        })

    def test_search_filename_stem_finds_file(self):
        # Filename stem is a token, so a query for ``InvoiceTests`` finds the
        # test file even though no class named ``InvoiceTests`` is what we're
        # querying — we hit on filename.
        self.assertEqual(self._rels("InvoiceTests"),
                         {"tests/billing/InvoiceTests.cs"})

    def test_search_full_filename_also_works(self):
        self.assertEqual(self._rels("Invoice.cs"), {
            "services/billing/Invoice.cs",
            "services/orders/legacy/Invoice.cs",
        })

    def test_extension_alone_matches(self):
        # ``cs`` is stored as a path_token from every .cs filename.
        self.assertEqual(len(self._rels("cs")), 3)


# ── End-to-end: namespace component search ────────────────────────────────────

class TestNamespaceComponentSearch(unittest.TestCase):
    """A dot-separated namespace is stored as multi-value raw — a search for
    any component returns the file."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="ts_ns_tokens_test_")
        self.backend, self._cleanup = make_test_backend()

        files = {
            "src/Order.cs":   "namespace Acme.Billing.Service { public class Order {} }",
            "src/Widget.cs":  "namespace Acme.Storage.Service { public class Widget {} }",
            "src/Plain.cs":   "namespace OtherCompany { public class Plain {} }",
        }
        for rel, body in files.items():
            full = os.path.join(self.workdir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(body)
            self.backend.upsert_many([build_document(full, rel)])

    def tearDown(self):
        self._cleanup()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def _rels(self, q: str) -> set[str]:
        result = _search(self.backend, q=q, query_by="namespace", per_page=20)
        return {h["document"]["relative_path"] for h in result.get("hits", [])}

    def test_top_level_namespace_component(self):
        self.assertEqual(self._rels("Acme"), {"src/Order.cs", "src/Widget.cs"})

    def test_middle_namespace_component(self):
        self.assertEqual(self._rels("Billing"), {"src/Order.cs"})
        self.assertEqual(self._rels("Storage"), {"src/Widget.cs"})

    def test_leaf_namespace_component_matches_both(self):
        # Both files end in ``.Service`` — the leaf is shared.
        self.assertEqual(self._rels("Service"),
                         {"src/Order.cs", "src/Widget.cs"})

    def test_unrelated_namespace(self):
        self.assertEqual(self._rels("OtherCompany"), {"src/Plain.cs"})

    def test_full_dotted_namespace_does_not_match(self):
        # Raw tokens store each component separately; the full dotted form
        # was never one stored term.
        self.assertEqual(self._rels("Acme.Billing.Service"), set())


# ── Case sensitivity at index level ──────────────────────────────────────────

class TestRawTokenizerIsCaseSensitive(unittest.TestCase):
    """Per-identifier fields use the raw tokenizer — index lookups are now
    case-sensitive. Wrong-case queries return zero index hits (the AST stage
    would also reject them, so end-user behavior is unchanged)."""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="ts_case_test_")
        self.backend, self._cleanup = make_test_backend()
        full = os.path.join(self.workdir, "Sample.cs")
        with open(full, "w", encoding="utf-8") as f:
            f.write("namespace N { public class SaveChanges { } }")
        self.backend.upsert_many([build_document(full, "Sample.cs")])

    def tearDown(self):
        self._cleanup()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_exact_case_matches(self):
        r = _search(self.backend, q="SaveChanges",
                    query_by="class_names", per_page=5)
        self.assertEqual(len(r.get("hits", [])), 1)

    def test_wrong_case_does_not_match(self):
        r = _search(self.backend, q="savechanges",
                    query_by="class_names", per_page=5)
        self.assertEqual(len(r.get("hits", [])), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
