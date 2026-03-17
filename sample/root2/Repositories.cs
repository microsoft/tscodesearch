// Repositories.cs -- repository types and inventory management.
// Covers: attrs, symbols (class/method names), usings, sig, param_type
using System;
using System.Collections.Generic;

namespace Sample.Repositories
{
    // -- Product repository ---------------------------------------------------

    // attrs: decorated with [Cacheable]
    [Cacheable(ttl: 60)]
    public class ProductRepository
    {
        public Product GetById(int id) { return null; }
        public void Save(Product p) { }
    }

    // attrs: decorated with [Obsolete], NOT [Cacheable]
    [Obsolete("Use NewRepository instead")]
    public class LegacyRepository
    {
        public void Fetch() { }
    }

    // attrs: no attributes at all
    public class PlainRepository
    {
        public void Fetch() { }
        public void Save() { }
    }

    // -- Inventory management -------------------------------------------------

    // symbols: class named InventoryManager
    public class InventoryManager
    {
        public void AddItem(string sku) { }
        public void RemoveItem(string sku) { }
    }

    // symbols: method named ProcessInventory
    public class WarehouseService
    {
        public void ProcessInventory(IList<string> skus) { }
        private void Audit(string sku) { }
    }

    // symbols: "InventoryManager" only in a string literal — content field but NOT symbols
    public class Config
    {
        public string ServiceName = "InventoryManager";
        public void Configure() { }
    }

    // -- Sig mode samples -----------------------------------------------------

    // sig: BlobStore in a parameter type and as a return type
    public class DataPipeline
    {
        public void Store(BlobStore bs, string key) { }
        public BlobStore Retrieve(string key) { return null; }
        private void LogEntry(string msg) { }
        private void WriteTag(int tag, string msg) { }
    }

    // sig: BlobStore only as a field name ("_blobStore")
    public class DataConsumer
    {
        private BlobStore _blobStore;
        public DataConsumer(BlobStore store) { _blobStore = store; }
        public void Run() { _blobStore.Write("key", new byte[0]); }
        public void WriteTag(int tag, string msg) { }
        public void LogEntry(string msg) { }
    }

    // -- Using alias sample ---------------------------------------------------

    // usings: using alias
    // (alias directive at file level is in SynthTypes.cs; this class uses BS type)
    public class AliasUser
    {
        private BlobStore _store;
        public void Run(BlobStore store) { _store = store; }
    }

    // -- Reporter -------------------------------------------------------------

    // calls IBlobService methods — no BlobStore in any declaration
    public class Reporter
    {
        private IBlobService _svc;

        public Reporter(IBlobService svc) { _svc = svc; }

        public void Report(string key)
        {
            var data = _svc.FetchBlob(key);
            LogResult(data);
        }

        private void LogResult(byte[] data) { }
        private void WriteTag(int id, string msg) { }
    }
}
