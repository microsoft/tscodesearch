"""
End-to-end integration tests using the checked-in sample/ directory.

Each test class calls run_index() in setUpClass to create a fresh Tantivy
index for sample/root1 or sample/root2, then drops it in tearDownClass.

sample/ layout
──────────────
  root1/  Processors.cs  DataStore.cs  BlobStorage.cs  services.py  pipeline.py
          query_fixture.cs  query_fixture.rs  query_fixture.js
          query_fixture.ts  query_fixture.cpp
  root2/  Widgets.cs     Repositories.cs  SynthTypes.cs  models.py   notifier.py
"""
from __future__ import annotations

import os
import sys
import time
import unittest

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _root not in sys.path:
    sys.path.insert(0, _root)

# ── Sample directory paths ────────────────────────────────────────────────────

SAMPLE_ROOT1 = os.path.join(_root, "sample", "root1")
SAMPLE_ROOT2 = os.path.join(_root, "sample", "root2")

_CONFIG_PATH = os.path.join(_root, "config.json")

# ── Connection config ─────────────────────────────────────────────────────────

from indexserver.config import load_config as _load_config
_e2e_cfg = _load_config()


def _require_server() -> None:
    """Tantivy is in-process; nothing external to require."""
    return None

# ── Helpers ───────────────────────────────────────────────────────────────────

def _search(collection: str, q: str,
            query_by: str = "filename,class_names,method_names,tokens",
            per_page: int = 20) -> list[dict]:
    from indexserver.indexer import ensure_backend
    from indexserver.search import search as _backend_search
    backend = ensure_backend(_e2e_cfg, collection, write=False)
    try:
        result = _backend_search(
            backend, q=q, query_by=query_by, per_page=per_page, num_typos=0,
        )
    finally:
        backend.close()
    return [h["document"] for h in result.get("hits", [])]


def _get_doc(collection: str, filename: str) -> dict | None:
    hits = _search(collection, os.path.splitext(filename)[0], per_page=10)
    return next((h for h in hits if h.get("filename") == filename), None)


def _collection_info(collection: str) -> dict | None:
    """Return info for an existing Tantivy index, or None if not yet created."""
    from indexserver.config import index_root
    from indexserver.backend import Backend
    root = next((r for r in _e2e_cfg.roots.values() if r.collection == collection), None)
    index_dir = root.index_dir if root else str(index_root() / collection)
    if not os.path.exists(os.path.join(index_dir, "meta.json")):
        return None
    try:
        backend = Backend(index_dir, write=False, create=False)
        info = {"num_documents": backend.num_documents()}
        backend.close()
        return info
    except Exception:
        return None


def _delete_collection(collection: str) -> None:
    from indexserver.backend import drop
    from indexserver.config import index_root
    root = next((r for r in _e2e_cfg.roots.values() if r.collection == collection), None)
    if root is None:
        drop(str(index_root() / collection))
    else:
        drop(root.index_dir)


def _count_sample_files(src_root: str) -> int:
    """Count indexable files in a sample directory using the same logic as the indexer."""
    from indexserver.indexer import walk_source_files
    return sum(1 for _ in walk_source_files(src_root, _e2e_cfg))


# ── TestSampleRoot1E2E ────────────────────────────────────────────────────────

class TestSampleRoot1E2E(unittest.TestCase):
    """E2E: index sample/root1 and verify search + semantic fields."""

    coll: str

    @classmethod
    def setUpClass(cls):
        _require_server()
        from indexserver.indexer import run_index
        cls.coll = f"test_e2e_r1_{int(time.time())}"
        run_index(_e2e_cfg, src_root=SAMPLE_ROOT1, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "coll"):
            _delete_collection(cls.coll)

    # ── File-level ────────────────────────────────────────────────────────────

    def test_collection_has_ten_files(self):
        info = _collection_info(self.coll)
        self.assertIsNotNone(info, f"Collection {self.coll!r} not found")
        ndocs = info["num_documents"]
        expected = _count_sample_files(SAMPLE_ROOT1)
        self.assertEqual(ndocs, expected,
            f"Expected {expected} docs in root1, got {ndocs}")

    def test_processors_cs_indexed(self):
        self.assertIsNotNone(_get_doc(self.coll, "Processors.cs"),
                             "Processors.cs not found in index")

    def test_datastore_cs_indexed(self):
        self.assertIsNotNone(_get_doc(self.coll, "DataStore.cs"),
                             "DataStore.cs not found in index")

    def test_blobstorage_cs_indexed(self):
        self.assertIsNotNone(_get_doc(self.coll, "BlobStorage.cs"),
                             "BlobStorage.cs not found in index")

    def test_services_py_indexed(self):
        hits = _search(self.coll, "services", query_by="filename,tokens")
        self.assertIn("services.py", [h["filename"] for h in hits],
                      "services.py not found in index")

    def test_pipeline_py_indexed(self):
        hits = _search(self.coll, "pipeline", query_by="filename,tokens")
        self.assertIn("pipeline.py", [h["filename"] for h in hits],
                      "pipeline.py not found in index")

    # ── Semantic fields: Processors.cs ───────────────────────────────────────

    def test_processors_base_types_has_iprocessor(self):
        doc = _get_doc(self.coll, "Processors.cs")
        self.assertIsNotNone(doc)
        bt = doc.get("base_types", [])
        self.assertIn("IProcessor", bt,
            f"Expected IProcessor in base_types (BaseProcessor : IProcessor<T>): {bt}")

    def test_processors_base_types_has_baseprocessor(self):
        doc = _get_doc(self.coll, "Processors.cs")
        self.assertIsNotNone(doc)
        bt = doc.get("base_types", [])
        self.assertIn("BaseProcessor", bt,
            f"Expected BaseProcessor in base_types (TextProcessor : BaseProcessor<string>): {bt}")

    def test_processors_attr_names_has_serializable(self):
        doc = _get_doc(self.coll, "Processors.cs")
        self.assertIsNotNone(doc)
        attrs = doc.get("attr_names", [])
        self.assertIn("Serializable", attrs,
            f"Expected Serializable attribute on BaseProcessor: {attrs}")

    def test_processors_attr_names_has_obsolete(self):
        doc = _get_doc(self.coll, "Processors.cs")
        self.assertIsNotNone(doc)
        attrs = doc.get("attr_names", [])
        self.assertIn("Obsolete", attrs,
            f"Expected Obsolete attribute on TextProcessor: {attrs}")

    def test_processors_call_sites_has_process(self):
        doc = _get_doc(self.coll, "Processors.cs")
        self.assertIsNotNone(doc)
        cs = doc.get("call_sites", [])
        self.assertIn("Process", cs,
            f"Expected Process in call_sites (processor.Process(input)): {cs}")

    def test_processors_call_sites_has_create(self):
        doc = _get_doc(self.coll, "Processors.cs")
        self.assertIsNotNone(doc)
        cs = doc.get("call_sites", [])
        self.assertIn("Create", cs,
            f"Expected Create in call_sites (ProcessorFactory.Create(...)): {cs}")

    def test_processors_usings_has_system(self):
        doc = _get_doc(self.coll, "Processors.cs")
        self.assertIsNotNone(doc)
        usings = doc.get("usings", [])
        self.assertIn("System", usings, f"Expected System in usings: {usings}")

    def test_processors_class_names_has_textprocessor(self):
        doc = _get_doc(self.coll, "Processors.cs")
        self.assertIsNotNone(doc)
        cn = doc.get("class_names", [])
        self.assertIn("TextProcessor", cn, f"class_names: {cn}")

    # ── Semantic fields: DataStore.cs ─────────────────────────────────────────

    def test_datastore_base_types_has_idatastore(self):
        doc = _get_doc(self.coll, "DataStore.cs")
        self.assertIsNotNone(doc)
        bt = doc.get("base_types", [])
        self.assertIn("IDataStore", bt,
            f"Expected IDataStore in base_types (SqlDataStore : IDataStore): {bt}")

    def test_datastore_base_types_has_idisposable(self):
        doc = _get_doc(self.coll, "DataStore.cs")
        self.assertIsNotNone(doc)
        bt = doc.get("base_types", [])
        self.assertIn("IDisposable", bt,
            f"Expected IDisposable in base_types (SqlDataStore : ..., IDisposable): {bt}")

    def test_datastore_type_refs_has_idatastore(self):
        doc = _get_doc(self.coll, "DataStore.cs")
        self.assertIsNotNone(doc)
        refs = doc.get("type_refs", [])
        self.assertIn("IDataStore", refs,
            f"Expected IDataStore in type_refs (fields, params, local vars): {refs}")

    # ── Semantic fields: BlobStorage.cs ──────────────────────────────────────

    def test_blobstorage_class_names_has_blobstore(self):
        doc = _get_doc(self.coll, "BlobStorage.cs")
        self.assertIsNotNone(doc)
        cn = doc.get("class_names", [])
        self.assertIn("BlobStore", cn, f"class_names: {cn}")

    def test_blobstorage_type_refs_has_blobstore(self):
        doc = _get_doc(self.coll, "BlobStorage.cs")
        self.assertIsNotNone(doc)
        refs = doc.get("type_refs", [])
        self.assertIn("BlobStore", refs,
            f"Expected BlobStore in type_refs (field, param, cast, return types): {refs}")

    # ── Search-mode queries ───────────────────────────────────────────────────

    def test_implements_search_finds_processors_for_iprocessor(self):
        hits = _search(self.coll, "IProcessor", query_by="base_types,class_names,filename")
        self.assertIn("Processors.cs", [h["filename"] for h in hits],
            "Processors.cs not found via base_types search for IProcessor")

    def test_implements_search_finds_datastore_for_idatastore(self):
        hits = _search(self.coll, "IDataStore", query_by="base_types,class_names,filename")
        self.assertIn("DataStore.cs", [h["filename"] for h in hits],
            "DataStore.cs not found via base_types search for IDataStore")

    def test_attrs_search_finds_processors_for_serializable(self):
        hits = _search(self.coll, "Serializable", query_by="attr_names,filename")
        self.assertIn("Processors.cs", [h["filename"] for h in hits],
            "Processors.cs not found via attr_names search for Serializable")

    def test_calls_search_finds_processors_for_create(self):
        hits = _search(self.coll, "Create", query_by="call_sites,filename")
        self.assertIn("Processors.cs", [h["filename"] for h in hits],
            "Processors.cs not found via call_sites search for Create")

    def test_uses_search_finds_datastore_for_blobstore(self):
        hits = _search(self.coll, "BlobStore", query_by="type_refs,class_names,filename")
        self.assertIn("DataStore.cs", [h["filename"] for h in hits],
            "DataStore.cs not found via type_refs search for BlobStore")

    # ── Root isolation ────────────────────────────────────────────────────────

    def test_root1_does_not_have_widgetservice(self):
        hits = _search(self.coll, "WidgetService")
        self.assertNotIn("Widgets.cs", [h["filename"] for h in hits],
            "Widgets.cs must not appear in root1 collection")


# ── TestSampleRoot2E2E ────────────────────────────────────────────────────────

class TestSampleRoot2E2E(unittest.TestCase):
    """E2E: index sample/root2 and verify search + semantic fields."""

    coll: str

    @classmethod
    def setUpClass(cls):
        _require_server()
        from indexserver.indexer import run_index
        cls.coll = f"test_e2e_r2_{int(time.time())}"
        run_index(_e2e_cfg, src_root=SAMPLE_ROOT2, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "coll"):
            _delete_collection(cls.coll)

    # ── File-level ────────────────────────────────────────────────────────────

    def test_collection_has_five_files(self):
        info = _collection_info(self.coll)
        self.assertIsNotNone(info, f"Collection {self.coll!r} not found")
        ndocs = info["num_documents"]
        expected = _count_sample_files(SAMPLE_ROOT2)
        self.assertEqual(ndocs, expected,
            f"Expected {expected} docs in root2, got {ndocs}")

    def test_widgets_cs_indexed(self):
        self.assertIsNotNone(_get_doc(self.coll, "Widgets.cs"),
                             "Widgets.cs not found in index")

    def test_repositories_cs_indexed(self):
        self.assertIsNotNone(_get_doc(self.coll, "Repositories.cs"),
                             "Repositories.cs not found in index")

    def test_synthtypes_cs_indexed(self):
        self.assertIsNotNone(_get_doc(self.coll, "SynthTypes.cs"),
                             "SynthTypes.cs not found in index")

    def test_models_py_indexed(self):
        hits = _search(self.coll, "models", query_by="filename,tokens")
        self.assertIn("models.py", [h["filename"] for h in hits],
                      "models.py not found in index")

    def test_notifier_py_indexed(self):
        hits = _search(self.coll, "notifier", query_by="filename,tokens")
        self.assertIn("notifier.py", [h["filename"] for h in hits],
                      "notifier.py not found in index")

    # ── Semantic fields: Widgets.cs ───────────────────────────────────────────

    def test_widgets_base_types_has_iwidgetservice(self):
        doc = _get_doc(self.coll, "Widgets.cs")
        self.assertIsNotNone(doc)
        bt = doc.get("base_types", [])
        self.assertIn("IWidgetService", bt,
            f"Expected IWidgetService in base_types (WidgetService : IWidgetService): {bt}")

    def test_widgets_call_sites_has_fetchwidget(self):
        doc = _get_doc(self.coll, "Widgets.cs")
        self.assertIsNotNone(doc)
        cs = doc.get("call_sites", [])
        self.assertIn("FetchWidget", cs,
            f"Expected FetchWidget in call_sites (WidgetClient.Run): {cs}")

    def test_widgets_class_names_has_widgetclient(self):
        doc = _get_doc(self.coll, "Widgets.cs")
        self.assertIsNotNone(doc)
        cn = doc.get("class_names", [])
        self.assertIn("WidgetClient", cn, f"class_names: {cn}")

    # ── Semantic fields: Repositories.cs ──────────────────────────────────────

    def test_repositories_attr_names_has_cacheable(self):
        doc = _get_doc(self.coll, "Repositories.cs")
        self.assertIsNotNone(doc)
        attrs = doc.get("attr_names", [])
        self.assertIn("Cacheable", attrs,
            f"Expected Cacheable in attr_names (ProductRepository [Cacheable]): {attrs}")

    def test_repositories_attr_names_has_obsolete(self):
        doc = _get_doc(self.coll, "Repositories.cs")
        self.assertIsNotNone(doc)
        attrs = doc.get("attr_names", [])
        self.assertIn("Obsolete", attrs,
            f"Expected Obsolete in attr_names (LegacyRepository [Obsolete]): {attrs}")

    def test_repositories_class_names_has_inventorymanager(self):
        doc = _get_doc(self.coll, "Repositories.cs")
        self.assertIsNotNone(doc)
        cn = doc.get("class_names", [])
        self.assertIn("InventoryManager", cn, f"class_names: {cn}")

    def test_repositories_method_names_has_processinventory(self):
        doc = _get_doc(self.coll, "Repositories.cs")
        self.assertIsNotNone(doc)
        mn = doc.get("method_names", [])
        self.assertIn("ProcessInventory", mn,
            f"Expected ProcessInventory in method_names (WarehouseService): {mn}")

    def test_repositories_member_sigs_has_blobstore_param(self):
        doc = _get_doc(self.coll, "Repositories.cs")
        self.assertIsNotNone(doc)
        sigs = doc.get("member_sigs", [])
        self.assertTrue(any("BlobStore" in s for s in sigs),
            f"Expected a member_sig containing BlobStore (DataPipeline.Store/Retrieve): {sigs}")

    # ── Semantic fields: SynthTypes.cs ────────────────────────────────────────

    def test_synthtypes_class_names_has_findme(self):
        doc = _get_doc(self.coll, "SynthTypes.cs")
        self.assertIsNotNone(doc)
        cn = doc.get("class_names", [])
        self.assertIn("FindMe", cn, f"class_names: {cn}")

    def test_synthtypes_class_names_has_paramsdemo(self):
        doc = _get_doc(self.coll, "SynthTypes.cs")
        self.assertIsNotNone(doc)
        cn = doc.get("class_names", [])
        self.assertIn("ParamsDemo", cn, f"class_names: {cn}")

    def test_synthtypes_usings_has_system(self):
        doc = _get_doc(self.coll, "SynthTypes.cs")
        self.assertIsNotNone(doc)
        usings = doc.get("usings", [])
        self.assertGreater(len(usings), 0,
            "Expected at least one using directive in SynthTypes.cs")

    # ── Search-mode queries ───────────────────────────────────────────────────

    def test_implements_search_finds_widgets_for_iwidgetservice(self):
        hits = _search(self.coll, "IWidgetService", query_by="base_types,class_names,filename")
        self.assertIn("Widgets.cs", [h["filename"] for h in hits],
            "Widgets.cs not found via base_types search for IWidgetService")

    def test_calls_search_finds_widgets_for_fetchwidget(self):
        hits = _search(self.coll, "FetchWidget", query_by="call_sites,filename")
        self.assertIn("Widgets.cs", [h["filename"] for h in hits],
            "Widgets.cs not found via call_sites search for FetchWidget")

    def test_attrs_search_finds_repositories_for_cacheable(self):
        hits = _search(self.coll, "Cacheable", query_by="attr_names,filename")
        self.assertIn("Repositories.cs", [h["filename"] for h in hits],
            "Repositories.cs not found via attr_names search for Cacheable")

    def test_symbols_search_finds_repositories_for_inventorymanager(self):
        hits = _search(self.coll, "InventoryManager",
                       query_by="class_names,method_names,filename")
        self.assertIn("Repositories.cs", [h["filename"] for h in hits],
            "Repositories.cs not found via symbols search for InventoryManager")

    # ── Root isolation ────────────────────────────────────────────────────────

    def test_root2_does_not_have_processors_cs(self):
        hits = _search(self.coll, "BaseProcessor")
        self.assertNotIn("Processors.cs", [h["filename"] for h in hits],
            "Processors.cs must not appear in root2 collection")


# ── TestSampleNewLanguagesE2E ─────────────────────────────────────────────────

class TestSampleNewLanguagesE2E(unittest.TestCase):
    """E2E: verify Rust, JS, TS, C++ fixtures in root1 are indexed with correct fields."""

    coll: str

    @classmethod
    def setUpClass(cls):
        _require_server()
        from indexserver.indexer import run_index
        cls.coll = f"test_e2e_langs_{int(time.time())}"
        run_index(_e2e_cfg, src_root=SAMPLE_ROOT1, collection=cls.coll, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "coll"):
            _delete_collection(cls.coll)

    # ── Rust ──────────────────────────────────────────────────────────────────

    def test_rust_fixture_indexed(self):
        self.assertIsNotNone(_get_doc(self.coll, "query_fixture.rs"),
                             "query_fixture.rs not found in index")

    def test_rust_class_names_has_processresult(self):
        doc = _get_doc(self.coll, "query_fixture.rs")
        self.assertIsNotNone(doc)
        self.assertIn("ProcessResult", doc.get("class_names", []))

    def test_rust_class_names_has_processor_trait(self):
        doc = _get_doc(self.coll, "query_fixture.rs")
        self.assertIsNotNone(doc)
        self.assertIn("Processor", doc.get("class_names", []))

    def test_rust_method_names_has_create_processor(self):
        doc = _get_doc(self.coll, "query_fixture.rs")
        self.assertIsNotNone(doc)
        self.assertIn("create_processor", doc.get("method_names", []))

    def test_rust_base_types_has_processor(self):
        doc = _get_doc(self.coll, "query_fixture.rs")
        self.assertIsNotNone(doc)
        self.assertIn("Processor", doc.get("base_types", []))

    def test_rust_call_sites_has_process(self):
        doc = _get_doc(self.coll, "query_fixture.rs")
        self.assertIsNotNone(doc)
        self.assertIn("process", doc.get("call_sites", []))

    def test_rust_usings_has_std(self):
        doc = _get_doc(self.coll, "query_fixture.rs")
        self.assertIsNotNone(doc)
        self.assertIn("std", doc.get("usings", []))

    def test_rust_searchable_via_base_types(self):
        hits = _search(self.coll, "Processor", query_by="base_types,class_names,filename")
        self.assertIn("query_fixture.rs", [h["filename"] for h in hits])

    # ── JavaScript ────────────────────────────────────────────────────────────

    def test_js_fixture_indexed(self):
        self.assertIsNotNone(_get_doc(self.coll, "query_fixture.js"),
                             "query_fixture.js not found in index")

    def test_js_class_names_has_textprocessor(self):
        doc = _get_doc(self.coll, "query_fixture.js")
        self.assertIsNotNone(doc)
        self.assertIn("TextProcessor", doc.get("class_names", []))

    def test_js_method_names_has_createprocessor(self):
        doc = _get_doc(self.coll, "query_fixture.js")
        self.assertIsNotNone(doc)
        self.assertIn("createProcessor", doc.get("method_names", []))

    def test_js_base_types_has_processor(self):
        doc = _get_doc(self.coll, "query_fixture.js")
        self.assertIsNotNone(doc)
        self.assertIn("Processor", doc.get("base_types", []))

    def test_js_call_sites_has_process(self):
        doc = _get_doc(self.coll, "query_fixture.js")
        self.assertIsNotNone(doc)
        self.assertIn("process", doc.get("call_sites", []))

    def test_js_usings_has_events(self):
        doc = _get_doc(self.coll, "query_fixture.js")
        self.assertIsNotNone(doc)
        self.assertIn("events", doc.get("usings", []))

    def test_js_searchable_via_call_sites(self):
        hits = _search(self.coll, "createProcessor", query_by="call_sites,method_names,filename")
        self.assertIn("query_fixture.js", [h["filename"] for h in hits])

    # ── TypeScript ────────────────────────────────────────────────────────────

    def test_ts_fixture_indexed(self):
        self.assertIsNotNone(_get_doc(self.coll, "query_fixture.ts"),
                             "query_fixture.ts not found in index")

    def test_ts_class_names_has_textprocessor(self):
        doc = _get_doc(self.coll, "query_fixture.ts")
        self.assertIsNotNone(doc)
        self.assertIn("TextProcessor", doc.get("class_names", []))

    def test_ts_class_names_has_interface(self):
        doc = _get_doc(self.coll, "query_fixture.ts")
        self.assertIsNotNone(doc)
        self.assertIn("IProcessor", doc.get("class_names", []))

    def test_ts_base_types_has_baseprocessor(self):
        doc = _get_doc(self.coll, "query_fixture.ts")
        self.assertIsNotNone(doc)
        self.assertIn("BaseProcessor", doc.get("base_types", []))

    def test_ts_base_types_has_iprocessor(self):
        doc = _get_doc(self.coll, "query_fixture.ts")
        self.assertIsNotNone(doc)
        self.assertIn("IProcessor", doc.get("base_types", []))

    def test_ts_attr_names_has_serializable(self):
        doc = _get_doc(self.coll, "query_fixture.ts")
        self.assertIsNotNone(doc)
        self.assertIn("serializable", doc.get("attr_names", []))

    def test_ts_call_sites_has_process(self):
        doc = _get_doc(self.coll, "query_fixture.ts")
        self.assertIsNotNone(doc)
        self.assertIn("process", doc.get("call_sites", []))

    def test_ts_searchable_via_attr_names(self):
        hits = _search(self.coll, "serializable", query_by="attr_names,filename")
        self.assertIn("query_fixture.ts", [h["filename"] for h in hits])

    # ── C++ ───────────────────────────────────────────────────────────────────

    def test_cpp_fixture_indexed(self):
        self.assertIsNotNone(_get_doc(self.coll, "query_fixture.cpp"),
                             "query_fixture.cpp not found in index")

    def test_cpp_class_names_has_textprocessor(self):
        doc = _get_doc(self.coll, "query_fixture.cpp")
        self.assertIsNotNone(doc)
        self.assertIn("TextProcessor", doc.get("class_names", []))

    def test_cpp_class_names_has_processresult(self):
        doc = _get_doc(self.coll, "query_fixture.cpp")
        self.assertIsNotNone(doc)
        self.assertIn("ProcessResult", doc.get("class_names", []))

    def test_cpp_method_names_has_createprocessor(self):
        doc = _get_doc(self.coll, "query_fixture.cpp")
        self.assertIsNotNone(doc)
        self.assertIn("createProcessor", doc.get("method_names", []))

    def test_cpp_base_types_has_baseprocessor(self):
        doc = _get_doc(self.coll, "query_fixture.cpp")
        self.assertIsNotNone(doc)
        self.assertIn("BaseProcessor", doc.get("base_types", []))

    def test_cpp_call_sites_has_process(self):
        doc = _get_doc(self.coll, "query_fixture.cpp")
        self.assertIsNotNone(doc)
        self.assertIn("process", doc.get("call_sites", []))

    def test_cpp_usings_has_string(self):
        doc = _get_doc(self.coll, "query_fixture.cpp")
        self.assertIsNotNone(doc)
        self.assertIn("string", doc.get("usings", []))

    def test_cpp_searchable_via_base_types(self):
        hits = _search(self.coll, "BaseProcessor", query_by="base_types,class_names,filename")
        self.assertIn("query_fixture.cpp", [h["filename"] for h in hits])


# ── TestSampleMultiRootE2E ────────────────────────────────────────────────────

class TestSampleMultiRootE2E(unittest.TestCase):
    """E2E: verify root1 and root2 are independent, correctly-isolated collections."""

    coll_r1: str
    coll_r2: str

    @classmethod
    def setUpClass(cls):
        _require_server()
        from indexserver.indexer import run_index
        stamp = int(time.time())
        cls.coll_r1 = f"test_e2e_mr1_{stamp}"
        cls.coll_r2 = f"test_e2e_mr2_{stamp}"
        run_index(_e2e_cfg, src_root=SAMPLE_ROOT1, collection=cls.coll_r1, resethard=True, verbose=False)
        run_index(_e2e_cfg, src_root=SAMPLE_ROOT2, collection=cls.coll_r2, resethard=True, verbose=False)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "coll_r1"):
            _delete_collection(cls.coll_r1)
        if hasattr(cls, "coll_r2"):
            _delete_collection(cls.coll_r2)

    def test_both_collections_exist(self):
        self.assertIsNotNone(_collection_info(self.coll_r1),
                             f"root1 collection {self.coll_r1!r} not found")
        self.assertIsNotNone(_collection_info(self.coll_r2),
                             f"root2 collection {self.coll_r2!r} not found")

    def test_root1_has_processors(self):
        hits = _search(self.coll_r1, "TextProcessor", query_by="class_names,filename")
        self.assertIn("Processors.cs", [h["filename"] for h in hits],
            "Processors.cs should be in root1")

    def test_root2_has_widgets(self):
        hits = _search(self.coll_r2, "WidgetClient", query_by="class_names,filename")
        self.assertIn("Widgets.cs", [h["filename"] for h in hits],
            "Widgets.cs should be in root2")

    def test_root1_missing_widget_content(self):
        hits = _search(self.coll_r1, "WidgetClient", query_by="class_names,filename")
        self.assertNotIn("Widgets.cs", [h["filename"] for h in hits],
            "Widgets.cs must NOT appear in root1")

    def test_root2_missing_processor_content(self):
        hits = _search(self.coll_r2, "TextProcessor", query_by="class_names,filename")
        self.assertNotIn("Processors.cs", [h["filename"] for h in hits],
            "Processors.cs must NOT appear in root2")

    def test_root1_doc_count_equals_nine(self):
        info = _collection_info(self.coll_r1)
        self.assertIsNotNone(info)
        expected = _count_sample_files(SAMPLE_ROOT1)
        self.assertEqual(info["num_documents"], expected,
            f"root1 expected {expected} docs, got {info['num_documents']}")

    def test_root2_doc_count_equals_five(self):
        info = _collection_info(self.coll_r2)
        self.assertIsNotNone(info)
        expected = _count_sample_files(SAMPLE_ROOT2)
        self.assertEqual(info["num_documents"], expected,
            f"root2 expected {expected} docs, got {info['num_documents']}")


if __name__ == "__main__":
    unittest.main()
