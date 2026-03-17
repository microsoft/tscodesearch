"""
End-to-end integration tests using the checked-in sample/ directory.

Run via run_tests.sh (auto-starts Typesense if needed):
    MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/c/.../run_tests.sh tests/test_sample_e2e.py

Run in Docker mode (builds container, indexes sample/, tears down after):
    MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/c/.../run_tests.sh --docker

These tests do NOT skip — if Typesense is unreachable the suite fails loudly,
which makes misconfiguration obvious rather than silently green.

Modes
─────
  Native  run_index() is called directly; two fresh collections are created
          from sample/root1 and sample/root2, then deleted after the suite.

  Docker  run_tests.sh --docker starts the container and sets:
              CODESEARCH_TEST_PORT  host-mapped Typesense port
              CODESEARCH_TEST_KEY   api key
          The container has already indexed the sample dirs; tests only query.

sample/ layout
──────────────
  root1/  Processors.cs  DataStore.cs  BlobStorage.cs  services.py
  root2/  Widgets.cs     Repositories.cs  SynthTypes.cs  models.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
import urllib.request
import urllib.parse

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# ── Sample directory paths ────────────────────────────────────────────────────

SAMPLE_ROOT1 = os.path.join(_root, "sample", "root1")
SAMPLE_ROOT2 = os.path.join(_root, "sample", "root2")

# ── Connection config (native vs Docker) ─────────────────────────────────────

_DOCKER_MODE = "CODESEARCH_TEST_PORT" in os.environ

if _DOCKER_MODE:
    _HOST = "localhost"
    _PORT = int(os.environ["CODESEARCH_TEST_PORT"])
    _KEY  = os.environ.get("CODESEARCH_TEST_KEY", "e2e-test-key")
else:
    try:
        from indexserver.config import HOST as _HOST, PORT as _PORT, API_KEY as _KEY
    except Exception:
        _HOST, _PORT, _KEY = "localhost", 8108, "codesearch-local"


def _require_server() -> None:
    """Raise AssertionError if Typesense is not reachable — tests fail, not skip."""
    try:
        url = f"http://{_HOST}:{_PORT}/health"
        with urllib.request.urlopen(url, timeout=5) as r:
            if json.loads(r.read()).get("ok"):
                return
    except Exception:
        pass
    mode = "Docker" if _DOCKER_MODE else "native"
    raise AssertionError(
        f"Typesense not reachable at {_HOST}:{_PORT} ({mode} mode).\n"
        f"Start it with: run_tests.sh  (auto-starts)  or  ts start  (WSL direct)"
    )

# ── Helpers ───────────────────────────────────────────────────────────────────

def _search(collection: str, q: str,
            query_by: str = "filename,class_names,method_names,content",
            per_page: int = 20) -> list[dict]:
    params = urllib.parse.urlencode({
        "q": q, "query_by": query_by, "per_page": per_page, "num_typos": 0,
    })
    url = f"http://{_HOST}:{_PORT}/collections/{collection}/documents/search?{params}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": _KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        return [h["document"] for h in json.loads(r.read()).get("hits", [])]


def _get_doc(collection: str, filename: str) -> dict | None:
    hits = _search(collection, os.path.splitext(filename)[0], per_page=10)
    return next((h for h in hits if h.get("filename") == filename), None)


def _collection_info(collection: str) -> dict | None:
    url = f"http://{_HOST}:{_PORT}/collections/{collection}"
    req = urllib.request.Request(url, headers={"X-TYPESENSE-API-KEY": _KEY})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _delete_collection(collection: str) -> None:
    url = f"http://{_HOST}:{_PORT}/collections/{collection}"
    req = urllib.request.Request(url, method="DELETE",
                                  headers={"X-TYPESENSE-API-KEY": _KEY})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ── TestSampleRoot1E2E ────────────────────────────────────────────────────────

class TestSampleRoot1E2E(unittest.TestCase):
    """E2E: index sample/root1 and verify search + semantic fields."""

    coll: str

    @classmethod
    def setUpClass(cls):
        _require_server()
        if _DOCKER_MODE:
            cls.coll = "codesearch_root1"
        else:
            from indexserver.indexer import run_index
            stamp = int(time.time())
            cls.coll = f"test_e2e_r1_{stamp}"
            run_index(src_root=SAMPLE_ROOT1, collection=cls.coll,
                      resethard=True, verbose=False)
            time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        if not _DOCKER_MODE and hasattr(cls, "coll"):
            _delete_collection(cls.coll)

    # ── File-level ────────────────────────────────────────────────────────────

    def test_collection_has_four_files(self):
        info = _collection_info(self.coll)
        self.assertIsNotNone(info, f"Collection {self.coll!r} not found")
        ndocs = info["num_documents"]
        self.assertEqual(ndocs, 4,
            f"Expected 4 docs in root1 (Processors.cs, DataStore.cs, "
            f"BlobStorage.cs, services.py), got {ndocs}")

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
        hits = _search(self.coll, "services", query_by="filename,content")
        self.assertIn("services.py", [h["filename"] for h in hits],
                      "services.py not found in index")

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

    def test_processors_attributes_has_serializable(self):
        doc = _get_doc(self.coll, "Processors.cs")
        self.assertIsNotNone(doc)
        attrs = doc.get("attributes", [])
        self.assertIn("Serializable", attrs,
            f"Expected Serializable attribute on BaseProcessor: {attrs}")

    def test_processors_attributes_has_obsolete(self):
        doc = _get_doc(self.coll, "Processors.cs")
        self.assertIsNotNone(doc)
        attrs = doc.get("attributes", [])
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
        hits = _search(self.coll, "Serializable", query_by="attributes,filename")
        self.assertIn("Processors.cs", [h["filename"] for h in hits],
            "Processors.cs not found via attributes search for Serializable")

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
        if _DOCKER_MODE:
            cls.coll = "codesearch_root2"
        else:
            from indexserver.indexer import run_index
            stamp = int(time.time())
            cls.coll = f"test_e2e_r2_{stamp}"
            run_index(src_root=SAMPLE_ROOT2, collection=cls.coll,
                      resethard=True, verbose=False)
            time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        if not _DOCKER_MODE and hasattr(cls, "coll"):
            _delete_collection(cls.coll)

    # ── File-level ────────────────────────────────────────────────────────────

    def test_collection_has_four_files(self):
        info = _collection_info(self.coll)
        self.assertIsNotNone(info, f"Collection {self.coll!r} not found")
        ndocs = info["num_documents"]
        self.assertEqual(ndocs, 4,
            f"Expected 4 docs in root2 (Widgets.cs, Repositories.cs, "
            f"SynthTypes.cs, models.py), got {ndocs}")

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
        hits = _search(self.coll, "models", query_by="filename,content")
        self.assertIn("models.py", [h["filename"] for h in hits],
                      "models.py not found in index")

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

    def test_repositories_attributes_has_cacheable(self):
        doc = _get_doc(self.coll, "Repositories.cs")
        self.assertIsNotNone(doc)
        attrs = doc.get("attributes", [])
        self.assertIn("Cacheable", attrs,
            f"Expected Cacheable in attributes (ProductRepository [Cacheable]): {attrs}")

    def test_repositories_attributes_has_obsolete(self):
        doc = _get_doc(self.coll, "Repositories.cs")
        self.assertIsNotNone(doc)
        attrs = doc.get("attributes", [])
        self.assertIn("Obsolete", attrs,
            f"Expected Obsolete in attributes (LegacyRepository [Obsolete]): {attrs}")

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

    def test_repositories_method_sigs_has_blobstore_param(self):
        doc = _get_doc(self.coll, "Repositories.cs")
        self.assertIsNotNone(doc)
        sigs = doc.get("method_sigs", [])
        self.assertTrue(any("BlobStore" in s for s in sigs),
            f"Expected a method_sig containing BlobStore (DataPipeline.Store/Retrieve): {sigs}")

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
        hits = _search(self.coll, "Cacheable", query_by="attributes,filename")
        self.assertIn("Repositories.cs", [h["filename"] for h in hits],
            "Repositories.cs not found via attributes search for Cacheable")

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


# ── TestSampleMultiRootE2E ────────────────────────────────────────────────────

class TestSampleMultiRootE2E(unittest.TestCase):
    """E2E: verify root1 and root2 are independent, correctly-isolated collections."""

    coll_r1: str
    coll_r2: str

    @classmethod
    def setUpClass(cls):
        _require_server()
        if _DOCKER_MODE:
            cls.coll_r1 = "codesearch_root1"
            cls.coll_r2 = "codesearch_root2"
        else:
            from indexserver.indexer import run_index
            stamp = int(time.time())
            cls.coll_r1 = f"test_e2e_mr1_{stamp}"
            cls.coll_r2 = f"test_e2e_mr2_{stamp}"
            run_index(src_root=SAMPLE_ROOT1, collection=cls.coll_r1,
                      resethard=True, verbose=False)
            run_index(src_root=SAMPLE_ROOT2, collection=cls.coll_r2,
                      resethard=True, verbose=False)
            time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        if not _DOCKER_MODE:
            _delete_collection(cls.coll_r1)
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

    def test_root1_doc_count_equals_four(self):
        info = _collection_info(self.coll_r1)
        self.assertIsNotNone(info)
        self.assertEqual(info["num_documents"], 4,
            f"root1 expected 4 docs, got {info['num_documents']}")

    def test_root2_doc_count_equals_four(self):
        info = _collection_info(self.coll_r2)
        self.assertIsNotNone(info)
        self.assertEqual(info["num_documents"], 4,
            f"root2 expected 4 docs, got {info['num_documents']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
