// BlobStorage.cs -- blob storage types, casts, member accesses.
// Covers: casts, member_accesses, param_type, field_type, ident, usings
using System;
using Sample.Storage;

namespace Sample.Blob
{
    // Stub — IRouter is referenced in RequestHandler.Dispatch but defined nowhere else.
    public interface IRouter { void Route(string key); }

    // -- Core type ------------------------------------------------------------

    public class BlobStore
    {
        public long Size { get; }
        public void Write(string key, byte[] data) { }
        public byte[] Read(string key) { return null; }
        public void Flush() { }
        public static void Delete(string key) { }
        public static void Purge(string key) { }
    }

    public interface IBlobStore
    {
        byte[] Read(string key);
    }

    public interface IBlobService
    {
        byte[] FetchBlob(string key);
        void StoreBlob(string key, byte[] data);
    }

    // -- Casts ----------------------------------------------------------------

    // casts: explicit (BlobStore) cast and 'as BlobStore' pattern
    public class CastDemo
    {
        public BlobStore Downcast(IDataStore store)
        {
            var bs = (BlobStore)store;          // explicit cast
            var ok = store as BlobStore;        // as-cast (not an explicit cast)
            return bs;
        }

        public void HandleArray(IDataStore[] arr)
        {
            var first = (BlobStore)arr[0];      // cast of element access
        }
    }

    // casts: cast inside a conditional expression
    public class CondCast
    {
        public BlobStore Resolve(object obj)
        {
            return obj is BlobStore b ? b : (BlobStore)GetDefault();
        }

        private object GetDefault() { return null; }
    }

    // casts: BlobStore only via 'as' cast — no explicit (BlobStore)expr
    public class AsCastOnly
    {
        public BlobStore TryCast(object obj)
        {
            return obj as BlobStore;
        }
    }

    // casts: uses BlobStore as field/param but NO explicit cast
    public class NoCastDemo
    {
        private BlobStore _store;
        public void Run(BlobStore store) { _store = store; }
        public BlobStore Get() { return _store; }
    }

    // -- Member accesses ------------------------------------------------------

    // member_accesses: typed parameter 'BlobStore store' -> accesses .Write, .Size, .Flush
    public class AccessDemo
    {
        public void Run(BlobStore store)
        {
            store.Write("key", null);
            var size = store.Size;
            store.Flush();
        }

        public void Other(string s)
        {
            var len = s.Length;   // s is string, NOT BlobStore
        }
    }

    // member_accesses: typed field 'BlobStore _store' -> accesses .Read
    public class FieldAccess
    {
        private BlobStore _store;

        public FieldAccess(BlobStore store) { _store = store; }

        public byte[] Get(string key)
        {
            return _store.Read(key);
        }
    }

    // member_accesses: only calls methods via IBlobStore interface (not BlobStore directly)
    public class InterfaceAccess
    {
        private IBlobStore _svc;

        public void Fetch(string key)
        {
            var data = _svc.Read(key);
        }
    }

    // member_accesses: var-inferred: var x = new BlobStore() -> x.Write() tracked
    public class VarInferred
    {
        public void Create()
        {
            var store = new BlobStore();
            store.Write("k", null);
            store.Flush();
        }
    }

    // -- Param types ----------------------------------------------------------

    // param_type: multiple methods each taking BlobStore as a parameter
    public class RequestHandler
    {
        public void Handle(BlobStore store, string key) { store.Write(key, null); }
        public void Verify(string key, BlobStore store) { }
        public string Lookup(string key) { return null; }   // no BlobStore param
        public void Dispatch(IRouter router, BlobStore store) { }
    }

    // param_type: ref/out modifiers on BlobStore parameter
    public class RefTest
    {
        public void TryGet(string key, out BlobStore result) { result = null; }
        public void Exchange(ref BlobStore store) { }
    }

    // -- Ident ----------------------------------------------------------------

    // ident: BlobStore in many syntactic contexts
    public class IdentDemo
    {
        private BlobStore _store;                               // field
        public BlobStore GetStore() { return _store; }          // return type
        public void Set(BlobStore store) { _store = store; }    // param
        public void Create() { var x = new BlobStore(); }       // object creation
        public void Cast(object o) { var b = (BlobStore)o; }    // cast
    }

    // ident: BlobStore only inside a comment — must NOT be found by ident
    // Works with BlobStore internally
    /* Also uses a BlobStore for caching */
    public class CommentOnly
    {
        public void Run() { }
    }

    // -- Static calls only ----------------------------------------------------

    // BlobStore appears only as a static call target
    public class StaticCallOnly
    {
        public void Run(string key)
        {
            BlobStore.Delete(key);
            BlobStore.Purge(key);
        }
    }
}
