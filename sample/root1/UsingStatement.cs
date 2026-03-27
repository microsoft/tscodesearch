// Fixture for testing uses_kind=locals with various local variable declaration forms.
// No SPO code — generic types only.
// Covers: plain locals, using-statement, for-statement, foreach-statement.
using System;

namespace Sample
{
    // Connection extends Exception so it can appear in catch clauses;
    // implements IDisposable so it can appear in using statements.
    public class Connection : Exception, IDisposable
    {
        public void Open() { }
        public void Close() { }
        public void Dispose() { }
    }

    // Transaction likewise needs both for using() and catch().
    public class Transaction : Exception, IDisposable
    {
        public void Commit() { }
        public void Rollback() { }
        public void Dispose() { }
    }

    public class DataService
    {
        // Plain local declaration — must still be found (regression guard)
        public void PlainLocal()
        {
            Connection plain = new Connection();
            plain.Open();
        }

        // using-statement with typed variable
        public void WithUsing()
        {
            using (Connection conn = new Connection())
            {
                conn.Open();
            }
        }

        // for-statement with typed initializer variable
        public void ForLoop(Connection[] arr)
        {
            for (Connection cur = arr[0]; cur != null; cur = null)
            {
                cur.Open();
            }
        }

        // Two using-statements in one method
        public void TwoUsings()
        {
            using (Connection c1 = new Connection())
            using (Connection c2 = new Connection())
            {
                c1.Open();
                c2.Open();
            }
        }

        // foreach-statement with typed iteration variable
        public void ForeachLoop(Connection[] arr)
        {
            foreach (Connection item in arr)
            {
                item.Open();
            }
        }

        // foreach with var — must NOT appear (type is implicit, can't be resolved)
        public void ForeachVar(Connection[] arr)
        {
            foreach (var item in arr)
            {
                item.Open();
            }
        }

        // out variable with explicit type
        private static bool TryOpen(out Connection result)
        {
            result = new Connection();
            return true;
        }
        public void OutVar()
        {
            if (TryOpen(out Connection opened))
            {
                opened.Open();
            }
        }

        // typed tuple deconstruction: (Connection a, Connection b) = ...
        private static (Connection, Connection) MakePair() =>
            (new Connection(), new Connection());
        public void TupleDecon()
        {
            (Connection first, Connection second) = MakePair();
            first.Open();
            second.Open();
        }

        // catch clause with typed variable
        public void WithCatch()
        {
            try { }
            catch (Connection ex) { ex.Open(); }
        }

        // catch clause without variable name — must NOT produce a result (no binding)
        public void CatchNoVar()
        {
            try { }
            catch (Transaction) { }
        }

        // using-statement with a different type — must NOT appear in Connection results
        public void OtherType()
        {
            using (Transaction tx = new Transaction())
            {
                tx.Commit();
            }
        }
    }
}
