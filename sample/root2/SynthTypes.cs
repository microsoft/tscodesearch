// SynthTypes.cs -- miscellaneous types for find, params, usings, accesses_of.
// Covers: find, params, usings, accesses_of, ident
using System;
using System.Collections.Generic;
using System.Threading.Tasks;
using BS = Storage.BlobStore;

namespace Sample.Synth
{
    // -- Find mode ------------------------------------------------------------

    public class FindMe
    {
        public void TargetMethod(string key)
        {
            var result = Lookup(key);
        }

        private string Lookup(string key) { return null; }

        public void OtherMethod(int x) { }
    }

    public class AnotherClass
    {
        // Same method name in a different class
        public void TargetMethod(int x) { }
    }

    // -- Params mode ----------------------------------------------------------

    public class ParamsDemo
    {
        public void SimpleMethod(string key, int count, bool flag) { }
        public void WithDefaults(string key, int count = 10, bool flag = false) { }
        public void WithModifiers(ref string key, out int count) { count = 0; }
        public void NoParams() { }
        public ParamsDemo(string name, ILogger log) { }
    }

    // -- Usings ---------------------------------------------------------------

    // usings: multiple directives including alias (see file top)
    public class TaskWorker
    {
        public Task<IList<string>> RunAsync() { return null; }
    }

    // -- Accesses_of ----------------------------------------------------------

    // accesses_of: two accesses of .Status on Order
    public class OrderProcessor
    {
        private IOrderRepository _repo;
        private ILogger _log;

        public void Process(Order order)
        {
            var s = order.Status;
            _log.Info(order.Status.ToString());
            var name = order.Name;
        }
    }

    // accesses_of: qualified — order.Status vs log.Status
    public class Mixed
    {
        public void Run(Order order, ILogger log)
        {
            var s1 = order.Status;
            var s2 = log.Status;
        }
    }

    // accesses_of: no .Status access at all — must return empty
    public class NoStatus
    {
        public void Run(Order order)
        {
            var n = order.Name;
            order.Process();
        }
    }

    // -- Using alias ----------------------------------------------------------

    // Demonstrates BS alias (Storage.BlobStore -> BS declared at file top)
    public class BsAliasUser
    {
        private BS _store;
        public void Run(BS store) { _store = store; }
    }
}
