// query_fixture.cs -- synthetic C# file for query_cs test suite.
// Contains no project-specific references; safe for public use.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using StringList = System.Collections.Generic.List<string>;

namespace QueryFixture
{
    // -- Interfaces -----------------------------------------------------------

    public interface IProcessor<T>
    {
        T Process(T input);
        IEnumerable<T> ProcessBatch(IEnumerable<T> inputs);
        void Reset();
    }

    public interface ILogger
    {
        void Log(string message);
        void Warn(string message);
    }

    // -- Delegate -------------------------------------------------------------

    public delegate ProcessResult ProcessDelegate(string input);

    // -- Enum -----------------------------------------------------------------

    public enum ProcessingMode
    {
        Sequential,
        Parallel,
        Batch,
    }

    // -- Struct ---------------------------------------------------------------

    public struct ProcessResult
    {
        public bool Success { get; }
        public string Output { get; }
        public int ErrorCode { get; }

        public ProcessResult(bool success, string output, int errorCode)
        {
            Success = success;
            Output = output;
            ErrorCode = errorCode;
        }
    }

    // -- Abstract base class --------------------------------------------------

    [Serializable]
    public abstract class BaseProcessor<T> : IProcessor<T>
    {
        protected ILogger _logger;

        protected BaseProcessor(ILogger logger)
        {
            _logger = logger;
        }

        public abstract T Process(T input);

        public virtual IEnumerable<T> ProcessBatch(IEnumerable<T> inputs)
        {
            return inputs.Select(Process);
        }

        public void Reset() { }
    }

    // -- Concrete class -------------------------------------------------------

    [Serializable]
    [Obsolete("Use EnhancedProcessor instead")]
    public class TextProcessor : BaseProcessor<string>, IProcessor<string>
    {
        private readonly ILogger _log;

        public string Prefix { get; set; }

        public TextProcessor(string prefix, ILogger log) : base(log)
        {
            Prefix = prefix;
            _log = log;
        }

        public override string Process(string input)
        {
            return Prefix + input;
        }

        // Calls in comments should NOT be matched: COMMENT_CALL()
        public string Format(string input)
        {
            // Process() is mentioned here in a comment but not a real call
            string result = "IDENT_IN_STRING";
            return result + input;
        }
    }

    // -- Static factory class -------------------------------------------------

    public static class ProcessorFactory
    {
        public static TextProcessor Create(string prefix, ILogger logger)
        {
            return new TextProcessor(prefix, logger);
        }

        public static ProcessResult Run(IProcessor<string> processor, string input)
        {
            var output = processor.Process(input);
            return new ProcessResult(true, output, 0);
        }
    }

    // -- Service class --------------------------------------------------------

    public class ProcessingService
    {
        private readonly IProcessor<string> _processor;
        private ILogger _logger;

        public ProcessingService(IProcessor<string> processor, ILogger logger)
        {
            _processor = processor;
            _logger = logger;
        }

        // member_accesses: explicitly typed parameter
        public void LogResult(ProcessResult result)
        {
            if (result.Success)
                _logger.Log(result.Output);
            var code = result.ErrorCode;
        }

        // member_accesses: var x = new T(...)
        public void CreateAndInspect(ILogger log)
        {
            var proc = new TextProcessor("hello", log);
            var name = proc.Prefix;
        }

        // member_accesses: var arr = new T[n]  →  var x = arr[i]
        public void BatchProcess(string[] inputs)
        {
            var results = new ProcessResult[inputs.Length];
            for (int i = 0; i < inputs.Length; i++)
            {
                var item = results[i];
                _logger.Log(item.Output);
                var ok = item.Success;
                var code = item.ErrorCode;
            }
        }

        // member_accesses: var x = expr as T
        public void InspectCast(object obj)
        {
            var proc = obj as TextProcessor;
            if (proc != null)
            {
                var p = proc.Prefix;
            }
        }

        // member_accesses: var x = (T)expr
        public void HardCast(object obj)
        {
            var proc = (TextProcessor)obj;
            var p = proc.Prefix;
        }

        // calls: bare name and qualified name
        public void DoWork(string input)
        {
            _processor.Process(input);
            var result = ProcessorFactory.Run(_processor, input);
        }

        // calls: qualified  ProcessorFactory.Create
        public TextProcessor MakeProcessor(string prefix)
        {
            return ProcessorFactory.Create(prefix, _logger);
        }

        // casts: (TextProcessor)
        public TextProcessor GetTextProcessor()
        {
            return (TextProcessor)_processor;
        }

        // params: method with default value and multiple types
        public string Transform(string input, int maxLength, bool trim = false)
        {
            var s = input;
            if (trim) s = s.Trim();
            return s.Length > maxLength ? s.Substring(0, maxLength) : s;
        }

        // uses: IProcessor referenced as a type (not its declaration name)
        public bool TryProcess(IProcessor<string> proc, string value, out string output)
        {
            output = proc.Process(value);
            return output != null;
        }
    }

    // -- Partial-name class ---------------------------------------------------
    // Deliberately named ProcessResultSummary to verify ident mode does NOT
    // partially match it when searching for the identifier "ProcessResult".

    public class ProcessResultSummary
    {
        public string Label { get; set; }
        public int Count { get; set; }
    }

    // -- Corner-case patterns -------------------------------------------------

    public static class CornerCaseSamples
    {
        // params: method with no parameters
        public static void FlushAll() { }

        // params: method with out and ref modifiers
        public static bool TryGetFirst(IProcessor<string> source, out ProcessResult result, ref int attempts)
        {
            result = default;
            attempts++;
            return false;
        }

        // uses + typeof: ProcessResult referenced via typeof()
        public static Type GetResultType()
        {
            return typeof(ProcessResult);
        }

        // uses: two ProcessResult refs on the same line (deduplication test)
        public static ProcessResult Merge(ProcessResult a, ProcessResult b)
        {
            return a.Success ? a : b;
        }

        // casts: real cast is absent; only a comment mentions (TextProcessor)obj.
        // The cast query must NOT match lines inside comments.
        public static void CommentCast(object obj)
        {
            // (TextProcessor)obj is mentioned here only in a comment
            string s = obj.ToString();
        }

        // member_accesses: chained — result.Output.Length
        // .Length is a member of string, not ProcessResult; must NOT appear
        // in member_accesses results for ProcessResult.
        public static int GetOutputLength(ProcessResult result)
        {
            return result.Output.Length;
        }
    }
}
