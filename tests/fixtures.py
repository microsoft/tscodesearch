"""
Synthetic C# source fixtures for codesearch mode tests.

All type/method names are fictional -- nothing references the real codebase.
Each section provides a pair (or set) of files designed to distinguish:
  - the "true positive" case (mode should match this)
  - the "false positive trap" (mode must NOT match this)
"""
from __future__ import annotations


# ==============================================================================
# calls mode
# ==============================================================================

# OK CALLS FetchWidget twice -- should appear in call_sites.
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


# ==============================================================================
# implements mode
# ==============================================================================

# OK Implements two interfaces.
IMPLEMENTS_IDATASTORE = """\
namespace Synth {
    public class SqlDataStore : IDataStore, IDisposable {
        public void Write(string key, byte[] data) { }
        public byte[] Read(string key) { return null; }
        public void Dispose() { }
    }
}
"""

# NO Uses BlobStore as field/param but NO explicit cast.
USES_BLOBSTORE_NO_CAST = """\
namespace Synth {
    public class NoCastDemo {
        private BlobStore _store;
        public void Run(BlobStore store) { _store = store; }
        public BlobStore Get() { return _store; }
    }
}
"""


# ==============================================================================
# ident mode (semantic grep -- every non-comment/string occurrence)
# ==============================================================================

# OK BlobStore in many syntactic contexts: field decl, return type, param, creation.
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

# NO BlobStore ONLY inside a comment (should NOT be found by ident).
IDENT_COMMENT_ONLY = """\
namespace Synth {
    // Works with BlobStore internally
    /* Also uses a BlobStore for caching */
    public class CommentOnly {
        public void Run() { }
    }
}
"""

# NO BlobStore only in a string literal.
IDENT_STRING_ONLY = """\
namespace Synth {
    public class StringOnly {
        public string Description = \"Uses a BlobStore under the hood\";
        public void Configure() { }
    }
}
"""


# ==============================================================================
# find mode
# ==============================================================================

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


# ==============================================================================
# params mode
# ==============================================================================

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


# ==============================================================================
# member_accesses mode
# ==============================================================================

# OK Typed parameter 'BlobStore store' -> accesses .Write, .Size, .Flush.
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

# OK Typed field 'BlobStore _store' -> accesses .Read.
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

# NO Only calls methods via IBlobStore interface (not BlobStore directly).
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

# OK var-inferred: var x = new BlobStore() -> x.Write() should be tracked.
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

# -- accesses_of fixtures ------------------------------------------------------

# OK Two accesses of .Status on different receiver types.
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

# OK Qualified name: only Order.Status, not Logger.Status.
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

# OK No .Status access -- must return empty.
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
