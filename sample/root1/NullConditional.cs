// Fixture for testing null-conditional member access (?.member) patterns.
// No SPO code — generic types only.
// Covers: q_calls, q_accesses_on, q_accesses_of with ?. syntax.
namespace Sample
{
    public class Logger
    {
        public void LogInfo(string msg) { }
        public void LogError(string msg) { }
    }

    public class Result
    {
        public string Message;
        public int Code;
        public Logger Log;
    }

    public class Worker
    {
        private Logger _logger;
        private Result _result;

        // q_calls: Logger?.LogInfo — null-conditional method call
        public void DoWork(Result r)
        {
            r.Log?.LogInfo("starting");
            r.Log?.LogError("failed");
        }

        // q_accesses_on: r is typed Result; r?.Message is a ?.member access
        public string GetMessage(Result r)
        {
            return r?.Message;
        }

        // q_accesses_on: chained ?.member
        public int GetCode(Result r)
        {
            return r?.Code ?? 0;
        }

        // q_accesses_of: find all accesses of member "Message"
        public void Report(Result primary, Result secondary)
        {
            var a = primary?.Message;
            var b = secondary.Message;    // regular access (already worked)
        }

        // Regular (non-null-conditional) access — regression guard
        public string Direct(Result r)
        {
            return r.Message;
        }
    }
}
