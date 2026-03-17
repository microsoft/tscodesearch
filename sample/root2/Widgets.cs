// Widgets.cs -- widget domain types.
// Covers: calls, symbols (class/method names), implements, usings
using System;
using System.Collections.Generic;

namespace Sample.Widgets
{
    // -- Core type ------------------------------------------------------------

    public class Widget
    {
        public string Id { get; }
        public string Name { get; set; }

        public Widget(string id) { Id = id; }
    }

    // -- Interface ------------------------------------------------------------

    public interface IWidgetService
    {
        Widget FetchWidget(string id);
        void SaveWidget(Widget w);
    }

    // -- Service implementation -----------------------------------------------

    // implements: IWidgetService
    public class WidgetService : IWidgetService
    {
        public Widget FetchWidget(string id) { return null; }
        public void SaveWidget(Widget w) { }
    }

    // -- Client: calls FetchWidget twice --------------------------------------

    public class WidgetClient
    {
        private IWidgetService _ws;

        public WidgetClient(IWidgetService ws) { _ws = ws; }

        // calls: FetchWidget
        public void Run()
        {
            var w  = _ws.FetchWidget("id1");
            var w2 = _ws.FetchWidget("id2");
        }

        public void Ping() { }
    }

    // -- Factory: calls new Widget() -----------------------------------------

    // calls: Widget constructor
    public class WidgetFactory
    {
        public Widget Build(string id) { return new Widget(id); }
        public void Dispose() { }
    }

    // -- Processor: BlobStore in sigs + type_refs + base_types ---------------

    public interface IProcessor
    {
        void Execute(BlobStore store);
        BlobStore Load(string key);
    }

    // implements: IProcessor; field_type: BlobStore
    public class WidgetProcessor : IProcessor
    {
        private BlobStore _store;
        public string Tag { get; set; }

        public WidgetProcessor(BlobStore store) { _store = store; }
        public void Execute(BlobStore store) { }
        public BlobStore Load(string key) { return _store; }
        public void LogTag(int tag) { }
    }

    // -- Consumer: calls IBlobService methods only ---------------------------

    public class BlobConsumer
    {
        private IBlobService _svc;

        public BlobConsumer(IBlobService svc) { _svc = svc; }

        public void Run()
        {
            var result = _svc.FetchBlob("key");
            _svc.StoreBlob("key2", result);
        }

        public void WriteTag(int tag, string text) { }
        public void LogEvent(string evt) { }
    }
}
