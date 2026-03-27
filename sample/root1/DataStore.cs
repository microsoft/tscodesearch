// DataStore.cs -- data storage abstractions and implementations.
// Covers: implements, field_type, param_type, usings, ident, local var type_refs
using System;
using System.Collections.Generic;
using Sample.Blob;
using Sample.Processing;

namespace Sample.Storage
{
    // -- Interfaces -----------------------------------------------------------

    public interface IDataStore
    {
        void Write(string key, byte[] data);
        byte[] Read(string key);
        // Static methods used in StaticUser below (C# 8+ static interface members)
        static void Flush(string key) { }
        static void Reset() { }
    }

    // -- Implementations ------------------------------------------------------

    // implements: IDataStore, IDisposable
    public class SqlDataStore : IDataStore, IDisposable
    {
        public void Write(string key, byte[] data) { }
        public byte[] Read(string key) { return null; }
        public void Dispose() { }
    }

    // field_type: IDataStore field and property
    public class CachingProxy
    {
        private IDataStore _inner;
        public IDataStore Inner { get; private set; }

        public CachingProxy(IDataStore inner)
        {
            _inner = inner;
            Inner = inner;
        }

        public byte[] Get(string key) { return _inner.Read(key); }
    }

    // param_type: IDataStore as parameter
    public class DataTransfer
    {
        private IDataStore _src;

        public DataTransfer(IDataStore src) { _src = src; }

        public void Transfer(string key)
        {
            var data = _src.Read(key);
        }
    }

    // type_refs (local var): IDataStore as a local variable type inside a method body
    public class LocalVarUser
    {
        public void Execute(object raw)
        {
            IDataStore store = (IDataStore)GetStore(raw);
            store.Write("k", null);
        }

        private object GetStore(object o) { return o; }
    }

    // type_refs (static receiver): IDataStore used only as a static call receiver
    public class StaticUser
    {
        public void Run(string key)
        {
            IDataStore.Flush(key);
            IDataStore.Reset();
        }
    }

    // -- Storage pool ---------------------------------------------------------

    // field_type: generic IList<BlobStore>
    public class BlobPool
    {
        private IList<BlobStore> _pool;
        public IReadOnlyList<BlobStore> All { get; set; }

        public void Add(BlobStore store) { _pool.Add(store); }
    }

    // field_type: BlobStore field and property
    public class StorageOwner
    {
        private BlobStore _primary;
        public BlobStore Backup { get; set; }
        public string Name { get; set; }

        public void Archive() { }
    }

    // -- Logged service -------------------------------------------------------

    // field_type: ILogger field and property
    public class LoggedService
    {
        private ILogger _log;
        public ILogger Log { get; set; }

        public LoggedService(ILogger log) { _log = log; }
        public void Run() { _log.Log("running"); }
    }
}
