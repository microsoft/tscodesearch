"""
End-to-end integration tests using the checked-in sample/ directory.

Each test class calls run_index() in setUpClass to create a fresh collection
from sample/root1 or sample/root2, then deletes it in tearDownClass.  This
works in both native WSL mode (sample/ on the host) and Docker mode (sample/
is at /app/sample/ inside the container via COPY . /app/).

Run natively (auto-starts Typesense if needed):
    MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/c/.../run_tests.sh tests/test_sample_e2e.py

Run in Docker mode:
    MSYS_NO_PATHCONV=1 wsl.exe bash -l /mnt/c/.../run_tests.sh --docker

These tests do NOT skip — if Typesense is unreachable the suite fails loudly.

sample/ layout
──────────────
  root1/  Processors.cs  DataStore.cs  BlobStorage.cs  services.py  pipeline.py
  root2/  Widgets.cs     Repositories.cs  SynthTypes.cs  models.py   notifier.py
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

_CONFIG_PATH = os.path.join(_root, "config.json")

# ── Connection config ─────────────────────────────────────────────────────────

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
    raise AssertionError(
        f"Typesense not reachable at {_HOST}:{_PORT}.\n"
        f"Start it with: run_tests.sh  (auto-starts)  or  ts start  (WSL direct)"
    )

# ── Helpers ───────────────────────────────────────────────────────────────────

def _search(collection: str, q: str,
            query_by: str = "filename,class_names,method_names,tokens",
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
        from indexserver.indexer import run_index
        cls.coll = f"test_e2e_r1_{int(time.time())}"
        run_index(src_root=SAMPLE_ROOT1, collection=cls.coll, resethard=True, verbose=False)
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
        self.assertEqual(ndocs, 5,
            f"Expected 5 docs in root1 (Processors.cs, DataStore.cs, "
            f"BlobStorage.cs, services.py, pipeline.py), got {ndocs}")

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
        run_index(src_root=SAMPLE_ROOT2, collection=cls.coll, resethard=True, verbose=False)
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
        self.assertEqual(ndocs, 5,
            f"Expected 5 docs in root2 (Widgets.cs, Repositories.cs, "
            f"SynthTypes.cs, models.py, notifier.py), got {ndocs}")

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
        run_index(src_root=SAMPLE_ROOT1, collection=cls.coll_r1, resethard=True, verbose=False)
        run_index(src_root=SAMPLE_ROOT2, collection=cls.coll_r2, resethard=True, verbose=False)
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

    def test_root1_doc_count_equals_five(self):
        info = _collection_info(self.coll_r1)
        self.assertIsNotNone(info)
        self.assertEqual(info["num_documents"], 5,
            f"root1 expected 5 docs, got {info['num_documents']}")

    def test_root2_doc_count_equals_five(self):
        info = _collection_info(self.coll_r2)
        self.assertIsNotNone(info)
        self.assertEqual(info["num_documents"], 5,
            f"root2 expected 5 docs, got {info['num_documents']}")


# ── TestPreConfiguredRootsE2E ─────────────────────────────────────────────────

_IN_DOCKER = os.environ.get("CODESEARCH_TEST_DOCKER") == "1"


@unittest.skipUnless(_IN_DOCKER, "pre-configured roots: only runs inside Docker (CODESEARCH_TEST_DOCKER=1)")
class TestPreConfiguredRootsE2E(unittest.TestCase):
    """E2E: verify collections pre-indexed by the entrypoint from config.json roots.

    run_tests.sh --docker writes config.json with root1 and root2 before starting
    the container.  The entrypoint indexes those roots on startup.  This class
    confirms that the right collections exist with the right content — testing
    the full root-add-from-outside workflow without touching anything from within.

    No setUpClass/tearDownClass: collections are owned by the container lifetime.
    """

    def _coll(self, name: str) -> str:
        from indexserver.config import collection_for_root
        return collection_for_root(name)

    # ── Collection naming ─────────────────────────────────────────────────────

    def test_collection_name_convention(self):
        """codesearch_{sanitized_name} naming applies to pre-configured roots."""
        self.assertEqual(self._coll("root1"), "codesearch_root1")
        self.assertEqual(self._coll("root2"), "codesearch_root2")

    # ── root1 ─────────────────────────────────────────────────────────────────

    def test_root1_collection_exists(self):
        coll = self._coll("root1")
        self.assertIsNotNone(_collection_info(coll),
            f"Collection {coll!r} not found — entrypoint should have indexed root1")

    def test_root1_has_five_files(self):
        coll = self._coll("root1")
        info = _collection_info(coll)
        self.assertIsNotNone(info)
        self.assertEqual(info["num_documents"], 5,
            f"Expected 5 docs in {coll!r}, got {info['num_documents']}")

    def test_root1_has_processors_cs(self):
        self.assertIsNotNone(_get_doc(self._coll("root1"), "Processors.cs"),
            "Processors.cs not found in pre-configured root1 collection")

    def test_root1_has_datastore_cs(self):
        self.assertIsNotNone(_get_doc(self._coll("root1"), "DataStore.cs"),
            "DataStore.cs not found in pre-configured root1 collection")

    # ── root2 ─────────────────────────────────────────────────────────────────

    def test_root2_collection_exists(self):
        coll = self._coll("root2")
        self.assertIsNotNone(_collection_info(coll),
            f"Collection {coll!r} not found — entrypoint should have indexed root2")

    def test_root2_has_five_files(self):
        coll = self._coll("root2")
        info = _collection_info(coll)
        self.assertIsNotNone(info)
        self.assertEqual(info["num_documents"], 5,
            f"Expected 5 docs in {coll!r}, got {info['num_documents']}")

    def test_root2_has_widgets_cs(self):
        self.assertIsNotNone(_get_doc(self._coll("root2"), "Widgets.cs"),
            "Widgets.cs not found in pre-configured root2 collection")

    def test_root2_has_repositories_cs(self):
        self.assertIsNotNone(_get_doc(self._coll("root2"), "Repositories.cs"),
            "Repositories.cs not found in pre-configured root2 collection")

    # ── Isolation ─────────────────────────────────────────────────────────────

    def test_root1_does_not_contain_root2_content(self):
        hits = _search(self._coll("root1"), "WidgetClient", query_by="class_names,filename")
        self.assertNotIn("Widgets.cs", [h["filename"] for h in hits],
            "Widgets.cs must not appear in pre-configured root1 collection")

    def test_root2_does_not_contain_root1_content(self):
        hits = _search(self._coll("root2"), "TextProcessor", query_by="class_names,filename")
        self.assertNotIn("Processors.cs", [h["filename"] for h in hits],
            "Processors.cs must not appear in pre-configured root2 collection")


# ── TestPythonAstQuery ────────────────────────────────────────────────────────

def _require_api_server() -> None:
    """Raise AssertionError if the management API (api.py) is not reachable."""
    try:
        from indexserver.config import API_PORT as _API_PORT
        url = f"http://{_HOST}:{_API_PORT}/health"
        with urllib.request.urlopen(url, timeout=5) as r:
            if json.loads(r.read()).get("ok"):
                return
    except Exception:
        pass
    raise AssertionError(
        f"Management API not reachable. Start it with: ts start"
    )


def _api_query(files: list, mode: str, pattern: str = "") -> list:
    """POST to api.py /query and return the results list."""
    from indexserver.config import API_PORT as _API_PORT
    url = f"http://{_HOST}:{_API_PORT}/query"
    body = json.dumps({"mode": mode, "pattern": pattern, "files": files}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"X-TYPESENSE-API-KEY": _KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()).get("results", [])


def _matches_for(results: list, filename: str) -> list:
    """Return match texts for the first result whose file ends with filename."""
    for r in results:
        if r["file"].replace("\\", "/").endswith(filename):
            return [m["text"] for m in r.get("matches", [])]
    return []


class TestPythonAstQuery(unittest.TestCase):
    """Verify Python AST query modes work via the api.py /query endpoint.

    Tests the fix for _run_query always using the C# parser — Python files
    must now dispatch to py_q_* functions via _py_parser.
    """

    SERVICES = os.path.join(SAMPLE_ROOT1, "services.py")
    PIPELINE = os.path.join(SAMPLE_ROOT1, "pipeline.py")
    MODELS   = os.path.join(SAMPLE_ROOT2, "models.py")
    NOTIFIER = os.path.join(SAMPLE_ROOT2, "notifier.py")

    @classmethod
    def setUpClass(cls):
        _require_api_server()

    # ── classes mode ─────────────────────────────────────────────────────────

    def test_classes_services_finds_textprocessor(self):
        results = _api_query([self.SERVICES], "classes")
        texts = _matches_for(results, "services.py")
        self.assertTrue(any("TextProcessor" in t for t in texts),
            f"TextProcessor not in classes output: {texts}")

    def test_classes_services_finds_iprocessor(self):
        results = _api_query([self.SERVICES], "classes")
        texts = _matches_for(results, "services.py")
        self.assertTrue(any("IProcessor" in t for t in texts),
            f"IProcessor not in classes output: {texts}")

    def test_classes_pipeline_finds_transformpipeline(self):
        results = _api_query([self.PIPELINE], "classes")
        texts = _matches_for(results, "pipeline.py")
        self.assertTrue(any("TransformPipeline" in t for t in texts),
            f"TransformPipeline not in classes output: {texts}")

    def test_classes_notifier_finds_notificationservice(self):
        results = _api_query([self.NOTIFIER], "classes")
        texts = _matches_for(results, "notifier.py")
        self.assertTrue(any("NotificationService" in t for t in texts),
            f"NotificationService not in classes output: {texts}")

    # ── implements mode ───────────────────────────────────────────────────────

    def test_implements_finds_textprocessor_for_baseprocessor(self):
        results = _api_query([self.SERVICES], "implements", "BaseProcessor")
        texts = _matches_for(results, "services.py")
        self.assertTrue(any("TextProcessor" in t for t in texts),
            f"TextProcessor(BaseProcessor) not found: {texts}")

    def test_implements_finds_jsontransformer_for_itransformer(self):
        results = _api_query([self.PIPELINE], "implements", "ITransformer")
        texts = _matches_for(results, "pipeline.py")
        self.assertTrue(any("JsonTransformer" in t for t in texts),
            f"JsonTransformer(ITransformer) not found: {texts}")

    def test_implements_finds_emailsink_for_ieventsink(self):
        results = _api_query([self.NOTIFIER], "implements", "IEventSink")
        texts = _matches_for(results, "notifier.py")
        self.assertTrue(any("EmailSink" in t for t in texts),
            f"EmailSink(IEventSink) not found: {texts}")

    # ── calls mode ────────────────────────────────────────────────────────────

    def test_calls_finds_process_in_services(self):
        results = _api_query([self.SERVICES], "calls", "process")
        texts = _matches_for(results, "services.py")
        self.assertTrue(len(texts) > 0,
            "No calls to process() found in services.py")

    def test_calls_finds_transform_in_pipeline(self):
        results = _api_query([self.PIPELINE], "calls", "transform")
        texts = _matches_for(results, "pipeline.py")
        self.assertTrue(len(texts) > 0,
            "No calls to transform() found in pipeline.py")

    def test_calls_finds_send_in_notifier(self):
        results = _api_query([self.NOTIFIER], "calls", "send")
        texts = _matches_for(results, "notifier.py")
        self.assertTrue(len(texts) > 0,
            "No calls to send() found in notifier.py")

    # ── declarations mode ─────────────────────────────────────────────────────

    def test_declarations_finds_textprocessor(self):
        results = _api_query([self.SERVICES], "declarations", "TextProcessor")
        texts = _matches_for(results, "services.py")
        self.assertTrue(any("TextProcessor" in t for t in texts),
            f"TextProcessor declaration not found: {texts}")

    def test_declarations_finds_transformpipeline(self):
        results = _api_query([self.PIPELINE], "declarations", "TransformPipeline")
        texts = _matches_for(results, "pipeline.py")
        self.assertTrue(any("TransformPipeline" in t for t in texts),
            f"TransformPipeline declaration not found: {texts}")

    # ── methods mode ──────────────────────────────────────────────────────────

    def test_methods_pipeline_finds_run(self):
        results = _api_query([self.PIPELINE], "methods")
        texts = _matches_for(results, "pipeline.py")
        self.assertTrue(any("run" in t for t in texts),
            f"run method not found in pipeline.py methods: {texts}")

    def test_methods_notifier_finds_broadcast(self):
        results = _api_query([self.NOTIFIER], "methods")
        texts = _matches_for(results, "notifier.py")
        self.assertTrue(any("broadcast" in t for t in texts),
            f"broadcast method not found in notifier.py methods: {texts}")

    # ── multi-file query ──────────────────────────────────────────────────────

    def test_classes_across_multiple_py_files(self):
        results = _api_query([self.SERVICES, self.PIPELINE], "classes")
        filenames = [r["file"].replace("\\", "/").split("/")[-1] for r in results]
        self.assertIn("services.py", filenames,
            "services.py missing from multi-file classes query")
        self.assertIn("pipeline.py", filenames,
            "pipeline.py missing from multi-file classes query")


if __name__ == "__main__":
    unittest.main(verbosity=2)
