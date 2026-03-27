// Throttle.cs -- rate-limiting and exponential-backoff utilities.
// Covers: declarations, implements, calls, accesses_of, accesses_on, methods, classes, uses
using System;

namespace Sample.Throttle
{
    // -- Interface ------------------------------------------------------------

    public interface IRetryPolicy
    {
        TimeSpan Interval { get; }
        void RecordAttempt(bool succeeded);
    }

    // -- Exponential backoff --------------------------------------------------

    /// <summary>
    /// Capped exponential backoff: doubles Interval on failure, halves on success.
    /// </summary>
    public class ExponentialRetry : IRetryPolicy
    {
        private readonly TimeSpan _maxInterval;   // field typed as TimeSpan
        private readonly TimeSpan _minInterval;   // field typed as TimeSpan
        private const double _factor = 2.0;

        public TimeSpan Interval { get; private set; }  // property typed as TimeSpan

        public ExponentialRetry(TimeSpan max, TimeSpan min)
        {
            _maxInterval = max;
            _minInterval = min;
            Interval = min;
        }

        public void RecordAttempt(bool succeeded)
        {
            // accesses_on "TimeSpan": Interval is a property — accesses_on misses this line
            if (Interval.TotalMilliseconds == 0)
            {
                Interval = TimeSpan.FromMilliseconds(_factor);
                return;
            }

            if (succeeded)
            {
                // accesses_on "TimeSpan": _minInterval is a field — accesses_on finds this line
                Interval = TimeSpan.FromMilliseconds(
                    Math.Max(_minInterval.TotalMilliseconds, Interval.TotalMilliseconds / _factor));
            }
            else
            {
                // accesses_on "TimeSpan": _maxInterval is a field — accesses_on finds this line
                Interval = TimeSpan.FromMilliseconds(
                    Math.Min(_maxInterval.TotalMilliseconds, Interval.TotalMilliseconds * _factor));
            }
        }
    }

    // -- Fixed delay ----------------------------------------------------------

    public class FixedRetry : IRetryPolicy
    {
        public TimeSpan Interval { get; }

        public FixedRetry(TimeSpan interval)
        {
            Interval = interval;
        }

        public void RecordAttempt(bool succeeded) { }
    }

    // -- Runner ---------------------------------------------------------------

    public class RetryRunner
    {
        private readonly IRetryPolicy _policy;

        public RetryRunner(IRetryPolicy policy)
        {
            _policy = policy;
        }

        public bool Execute(Func<bool> action, int maxAttempts)
        {
            for (int i = 0; i < maxAttempts; i++)
            {
                bool ok = action();
                _policy.RecordAttempt(ok);          // accesses_of "RecordAttempt"
                if (ok) return true;
                System.Threading.Thread.Sleep(_policy.Interval);  // accesses_of "Interval"
            }
            return false;
        }
    }
}
