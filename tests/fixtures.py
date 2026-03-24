"""
Synthetic C# source fixtures for codesearch mode tests.

All type/method names are fictional — nothing references the real codebase.
Each section provides a pair (or set) of files designed to distinguish:
  - the "true positive" case (mode should match this)
  - the "false positive trap" (mode must NOT match this)
"""
from __future__ import annotations

# ══════════════════════════════════════════════════════════════════════════════
# sig / listing mode
# ══════════════════════════════════════════════════════════════════════════════

# ✓ BlobStore in a *parameter type* and as a *return type* — correct sig hit.
SIG_HAS_PARAM = """\
namespace Synth {
    public class DataPipeline {
        public void Store(BlobStore bs, string key) { }
        public BlobStore Retrieve(string key) { return null; }
        private void LogEntry(string msg) { }
        private void WriteTag(int tag, string msg) { }
    }
}
"""

# ✗ BlobStore only as a *field name* ("_blobStore").
#   DataConsumer's ctor DOES take BlobStore — use CALLS_ONLY for a purer case.
FIELD_NAME_ONLY = """\
namespace Synth {
    public class DataConsumer {
        private BlobStore _blobStore;
        public DataConsumer(BlobStore store) { _blobStore = store; }
        public void Run() { _blobStore.Write("key", new byte[0]); }
        public void WriteTag(int tag, string msg) { }
        public void LogEntry(string msg) { }
    }
}
"""

# ✗ BlobStore only in *call sites* — no method takes BlobStore as a param type.
CALLS_ONLY = """\
namespace Synth {
    public class BlobConsumer {
        private IBlobService _svc;
        public BlobConsumer(IBlobService svc) { _svc = svc; }
        public void Run() {
            var result = _svc.FetchBlob("key");
            _svc.StoreBlob("key2", result);
        }
        public void WriteTag(int tag, string text) { }
        public void LogEvent(string evt) { }
    }
}
"""

# ✗ Class *named* "BlobStoreMigrator" — class_names hit, no BlobStore in sigs.
NAME_CONTAINS = """\
namespace Synth {
    public class BlobStoreMigrator {
        private string _name;
        public void Migrate() { }
        public void Cancel() { }
    }
}
"""

# ✓ Used for listing tests: BlobStore in sigs + type_refs + base_types via IProcessor.
LISTING_TARGET = """\
namespace Synth {
    public interface IProcessor {
        void Execute(BlobStore store);
        BlobStore Load(string key);
    }
    public class WidgetProcessor : IProcessor {
        private BlobStore _store;
        public string Tag { get; set; }
        public WidgetProcessor(BlobStore store) { _store = store; }
        public void Execute(BlobStore store) { }
        public BlobStore Load(string key) { return _store; }
        public void LogTag(int tag) { }
    }
}
"""

# ✓ "BlobStore" appears only as a static call target — shows up in tokens but
#   NOT in member_sigs, type_refs, class_names, or base_types.
#   Used to verify that the broad pre-filter (tokens field) picks it up.
CONTENT_ONLY_BLOBSTORE = """\
namespace Synth {
    public class StaticCallOnly {
        public void Run(string key) {
            BlobStore.Delete(key);
            BlobStore.Purge(key);
        }
    }
}
"""

# ✗ Calls IBlobService methods — no BlobStore in any declaration.
CALLS_IBLOBSERVICE = """\
namespace Synth {
    public class Reporter {
        private IBlobService _svc;
        public Reporter(IBlobService svc) { _svc = svc; }
        public void Report(string key) {
            var data = _svc.FetchBlob(key);
            LogResult(data);
        }
        private void LogResult(byte[] data) { }
        private void WriteTag(int id, string msg) { }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# calls mode
# ══════════════════════════════════════════════════════════════════════════════

# ✓ CALLS FetchWidget twice — should appear in call_sites.
CALLS_FETCHWIDGET = """\
namespace Synth {
    public class WidgetClient {
        private IWidgetService _ws;
        public WidgetClient(IWidgetService ws) { _ws = ws; }
        public void Run() {
            var w  = _ws.FetchWidget("id1");
            var w2 = _ws.FetchWidget("id2");
        }
        public void Ping() { }
    }
}
"""

# ✗ DEFINES FetchWidget but never calls it.
DEFINES_FETCHWIDGET = """\
namespace Synth {
    public class WidgetService {
        public Widget FetchWidget(string id) { return null; }
        public void SaveWidget(Widget w) { }
    }
}
"""

# ✓ Constructor call new Widget() should appear in call_sites.
CALLS_WIDGET_CTOR = """\
namespace Synth {
    public class WidgetFactory {
        public Widget Build(string id) { return new Widget(id); }
        public void Dispose() { }
    }
}
"""

# ✗ Defines Widget constructor — definition must not be in call_sites.
DEFINES_WIDGET_CTOR = """\
namespace Synth {
    public class Widget {
        public string Id { get; }
        public Widget(string id) { Id = id; }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# implements mode
# ══════════════════════════════════════════════════════════════════════════════

# ✓ Implements two interfaces.
IMPLEMENTS_IDATASTORE = """\
namespace Synth {
    public class SqlDataStore : IDataStore, IDisposable {
        public void Write(string key, byte[] data) { }
        public byte[] Read(string key) { return null; }
        public void Dispose() { }
    }
}
"""

# ✗ Uses IDataStore as a *parameter* but does not implement it.
USES_IDATASTORE_PARAM = """\
namespace Synth {
    public class DataTransfer {
        private IDataStore _src;
        public DataTransfer(IDataStore src) { _src = src; }
        public void Transfer(string key) {
            var data = _src.Read(key);
        }
    }
}
"""

# ✓ Declares an IDataStore field and property — type_refs hit.
DECLARES_FIELD_IDATASTORE = """\
namespace Synth {
    public class CachingProxy {
        private IDataStore _inner;
        public IDataStore Inner { get; private set; }
        public CachingProxy(IDataStore inner) { _inner = inner; Inner = inner; }
        public byte[] Get(string key) { return _inner.Read(key); }
    }
}
"""

# ✗ IDataStore only in a comment — no declaration.
COMMENT_ONLY_IDATASTORE = """\
namespace Synth {
    // This class works with IDataStore indirectly via a helper.
    public class IndirectWorker {
        public void DoWork() { }
        private void Helper(string x) { }
    }
}
"""

# ✓ IDataStore only as a *local variable type* inside a method body.
#   No field, no param, no return type — tests that local decls go into type_refs.
LOCAL_VAR_IDATASTORE = """\
namespace Synth {
    public class LocalVarUser {
        public void Execute(object raw) {
            IDataStore store = GetStore(raw);
            store.Write("k", null);
        }
        private object GetStore(object o) { return o; }
    }
}
"""

# ✓ IDataStore used only as a *static call receiver* — IDataStore.Create().
#   No field/param/return type — tests that PascalCase static receivers go into type_refs.
STATIC_RECEIVER_IDATASTORE = """\
namespace Synth {
    public class StaticUser {
        public void Run(string key) {
            IDataStore.Flush(key);
            IDataStore.Reset();
        }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# attrs mode
# ══════════════════════════════════════════════════════════════════════════════

# ✓ Decorated with [Cacheable].
HAS_CACHEABLE_ATTR = """\
namespace Synth {
    [Cacheable(ttl: 60)]
    public class ProductRepository {
        public Product GetById(int id) { return null; }
        public void Save(Product p) { }
    }
}
"""

# ✗ Decorated with [Obsolete], NOT [Cacheable].
HAS_OBSOLETE_NOT_CACHEABLE = """\
namespace Synth {
    [Obsolete("Use NewRepository instead")]
    public class LegacyRepository {
        public void Fetch() { }
    }
}
"""

# ✗ No attributes at all.
NO_ATTRS = """\
namespace Synth {
    public class PlainRepository {
        public void Fetch() { }
        public void Save() { }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# symbols / text mode
# ══════════════════════════════════════════════════════════════════════════════

# ✓ Class *named* InventoryManager.
CLASS_NAMED_INVENTORYMANAGER = """\
namespace Synth {
    public class InventoryManager {
        public void AddItem(string sku) { }
        public void RemoveItem(string sku) { }
    }
}
"""

# ✓ Method *named* ProcessInventory.
METHOD_NAMED_PROCESSINVENTORY = """\
namespace Synth {
    public class WarehouseService {
        public void ProcessInventory(IList<string> skus) { }
        private void Audit(string sku) { }
    }
}
"""

# ✗ "InventoryManager" only in a string literal — content field but NOT symbols.
LITERAL_ONLY = """\
namespace Synth {
    public class Config {
        public string ServiceName = "InventoryManager";
        public void Configure() { }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# field_type mode
# ══════════════════════════════════════════════════════════════════════════════

# ✓ BlobStore declared as a field AND a property.
FIELD_TYPED_BLOBSTORE = """\
namespace Synth {
    public class StorageOwner {
        private BlobStore _primary;
        public BlobStore Backup { get; set; }
        public string Name { get; set; }
        public void Archive() { }
    }
}
"""

# ✗ BlobStore only as a *method parameter*, not as a field/property type.
PARAM_ONLY_BLOBSTORE = """\
namespace Synth {
    public class Processor {
        public void Process(BlobStore store, string key) { }
        public void Flush() { }
    }
}
"""

# ✗ Field declared with a different type (ILogger).
FIELD_TYPED_ILOGGER = """\
namespace Synth {
    public class LoggedService {
        private ILogger _log;
        public ILogger Log { get; set; }
        public LoggedService(ILogger log) { _log = log; }
        public void Run() { _log.Info("running"); }
    }
}
"""

# ✓ Generic field IList<BlobStore> — should expand 'BlobStore' individually.
FIELD_TYPED_GENERIC_BLOBSTORE = """\
namespace Synth {
    public class BlobPool {
        private IList<BlobStore> _pool;
        public IReadOnlyList<BlobStore> All { get; set; }
        public void Add(BlobStore store) { _pool.Add(store); }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# param_type mode
# ══════════════════════════════════════════════════════════════════════════════

# ✓ Multiple methods each taking BlobStore as a parameter.
PARAM_TYPED_BLOBSTORE_MULTI = """\
namespace Synth {
    public class RequestHandler {
        public void Handle(BlobStore store, string key) { store.Write(key, null); }
        public void Verify(string key, BlobStore store) { }
        public string Lookup(string key) { return null; }   // no BlobStore param
        public void Dispatch(IRouter router, BlobStore store) { }
    }
}
"""

# ✗ BlobStore only as a *field*, not in any method parameter list.
FIELD_ONLY_NO_PARAMS = """\
namespace Synth {
    public class Container {
        private BlobStore _store;
        public Container(string name) { }               // ctor: no BlobStore param
        public void Store(string key, byte[] data) { _store.Write(key, data); }
        public void Clear() { }
    }
}
"""

# ✓ ref/out/params modifiers on BlobStore parameter.
PARAM_TYPED_WITH_MODIFIERS = """\
namespace Synth {
    public class RefTest {
        public void TryGet(string key, out BlobStore result) { result = null; }
        public void Exchange(ref BlobStore store) { }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# casts mode
# ══════════════════════════════════════════════════════════════════════════════

# ✓ Explicit (BlobStore) cast and 'as BlobStore' pattern.
CASTS_TO_BLOBSTORE = """\
namespace Synth {
    public class CastDemo {
        public BlobStore Downcast(IDataStore store) {
            var bs = (BlobStore)store;          // explicit cast
            var ok = store as BlobStore;        // as-cast (not an explicit cast)
            return bs;
        }
        public void HandleArray(IDataStore[] arr) {
            var first = (BlobStore)arr[0];    // cast of element access
        }
    }
}
"""

# ✗ Uses BlobStore as field/param but NO explicit cast.
USES_BLOBSTORE_NO_CAST = """\
namespace Synth {
    public class NoCastDemo {
        private BlobStore _store;
        public void Run(BlobStore store) { _store = store; }
        public BlobStore Get() { return _store; }
    }
}
"""

# ✓ Cast inside a conditional expression.
CAST_IN_CONDITIONAL = """\
namespace Synth {
    public class CondCast {
        public BlobStore Resolve(object obj) {
            return obj is BlobStore b ? b : (BlobStore)GetDefault();
        }
        private object GetDefault() { return null; }
    }
}
"""

# ✗ BlobStore only via 'as' cast — NO explicit (BlobStore)expr cast.
#   cast_types must NOT contain BlobStore.
AS_CAST_ONLY_BLOBSTORE = """\
namespace Synth {
    public class AsCastOnly {
        public BlobStore TryCast(object obj) {
            return obj as BlobStore;
        }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# ident mode (semantic grep — every non-comment/string occurrence)
# ══════════════════════════════════════════════════════════════════════════════

# ✓ BlobStore in many syntactic contexts: field decl, return type, param, creation.
IDENT_BLOBSTORE_MANY_CONTEXTS = """\
namespace Synth {
    public class IdentDemo {
        private BlobStore _store;                           // field
        public BlobStore GetStore() { return _store; }      // return type
        public void Set(BlobStore store) { _store = store; } // param
        public void Create() { var x = new BlobStore(); }   // object creation
        public void Cast(object o) { var b = (BlobStore)o; } // cast
    }
}
"""

# ✗ BlobStore ONLY inside a comment (should NOT be found by ident).
IDENT_COMMENT_ONLY = """\
namespace Synth {
    // Works with BlobStore internally
    /* Also uses a BlobStore for caching */
    public class CommentOnly {
        public void Run() { }
    }
}
"""

# ✗ BlobStore only in a string literal.
IDENT_STRING_ONLY = """\
namespace Synth {
    public class StringOnly {
        public string Description = \"Uses a BlobStore under the hood\";
        public void Configure() { }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# usings mode
# ══════════════════════════════════════════════════════════════════════════════

# ✓ Multiple using directives including an alias.
USING_SYSTEM_GENERIC = """\
using System;
using System.Collections.Generic;
using System.Threading.Tasks;
namespace Synth {
    public class TaskWorker {
        public Task<IList<string>> RunAsync() { return null; }
    }
}
"""

USING_WITH_ALIAS = """\
using BS = Storage.BlobStore;
using System;
namespace Synth {
    public class AliasUser {
        private BS _store;
        public void Run(BS store) { _store = store; }
    }
}
"""

# ✗ No using directives.
NO_USINGS = """\
namespace Synth {
    public class Standalone {
        public void Run() { }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# find mode
# ══════════════════════════════════════════════════════════════════════════════

# File with multiple types/methods to locate by name.
FIND_TARGET = """\
namespace Synth {
    public class FindMe {
        public void TargetMethod(string key) {
            var result = Lookup(key);
        }
        private string Lookup(string key) { return null; }
        public void OtherMethod(int x) { }
    }

    public class AnotherClass {
        // Same method name in different class
        public void TargetMethod(int x) { }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# params mode
# ══════════════════════════════════════════════════════════════════════════════

PARAMS_TARGET = """\
namespace Synth {
    public class ParamsDemo {
        public void SimpleMethod(string key, int count, bool flag) { }
        public void WithDefaults(string key, int count = 10, bool flag = false) { }
        public void WithModifiers(ref string key, out int count) { count = 0; }
        public void NoParams() { }
        public ParamsDemo(string name, ILogger log) { }
    }
}
"""


# ══════════════════════════════════════════════════════════════════════════════
# member_accesses mode
# ══════════════════════════════════════════════════════════════════════════════

# ✓ Typed parameter 'BlobStore store' → accesses .Write, .Size, .Flush.
MEMBER_ACCESS_BLOBSTORE_PARAM = """\
namespace Synth {
    public class AccessDemo {
        public void Run(BlobStore store) {
            store.Write(\"key\", null);
            var size = store.Size;
            store.Flush();
        }
        public void Other(string s) {
            var len = s.Length;   // s is string, NOT BlobStore
        }
    }
}
"""

# ✓ Typed field 'BlobStore _store' → accesses .Read.
MEMBER_ACCESS_BLOBSTORE_FIELD = """\
namespace Synth {
    public class FieldAccess {
        private BlobStore _store;
        public FieldAccess(BlobStore store) { _store = store; }
        public byte[] Get(string key) {
            return _store.Read(key);
        }
    }
}
"""

# ✗ Only calls methods via IBlobStore interface (not BlobStore directly).
MEMBER_ACCESS_INTERFACE_ONLY = """\
namespace Synth {
    public class InterfaceAccess {
        private IBlobStore _svc;
        public void Fetch(string key) {
            var data = _svc.Read(key);
        }
    }
}
"""

# ✓ var-inferred: var x = new BlobStore() → x.Write() should be tracked.
MEMBER_ACCESS_VAR_INFERRED = """\
namespace Synth {
    public class VarInferred {
        public void Create() {
            var store = new BlobStore();
            store.Write(\"k\", null);
            store.Flush();
        }
    }
}
"""

# ── accesses_of fixtures ──────────────────────────────────────────────────────

# ✓ Two accesses of .Status on different receiver types.
ACCESSES_OF_STATUS = """\
namespace Synth {
    public class OrderProcessor {
        private IOrderRepository _repo;
        private ILogger _log;
        public void Process(Order order) {
            var s = order.Status;
            _log.Info(order.Status.ToString());
            var name = order.Name;
        }
    }
}
"""

# ✓ Qualified name: only Order.Status, not Logger.Status.
ACCESSES_OF_STATUS_QUALIFIED = """\
namespace Synth {
    public class Mixed {
        public void Run(Order order, ILogger log) {
            var s1 = order.Status;
            var s2 = log.Status;
        }
    }
}
"""

# ✓ No .Status access — must return empty.
ACCESSES_OF_NO_STATUS = """\
namespace Synth {
    public class NoStatus {
        public void Run(Order order) {
            var n = order.Name;
            order.Process();
        }
    }
}
"""
