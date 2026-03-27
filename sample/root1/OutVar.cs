// Fixture for testing accesses_on with inline out variable declarations.
// No SPO code — generic types only.
// Pattern: method(args, out SomeType varName) declares a new variable inline.
using System.Collections.Generic;

namespace Sample
{
    public class Token { public string Value; public int Length; }

    public class Parser
    {
        // out bool return pattern -- helper that outputs a Token
        private static bool TryParse(string input, out Token result)
        {
            result = new Token();
            return true;
        }

        // Inline out variable: out Token tok declared inside the if condition
        public string Parse(string input)
        {
            if (TryParse(input, out Token tok))
            {
                return tok.Value + tok.Length.ToString();
            }
            return null;
        }

        // out var (var-inferred) — type cannot be determined without inference
        public string ParseVar(string input)
        {
            if (TryParse(input, out var entry))
            {
                return entry.Value;
            }
            return null;
        }

        // Plain local for regression: Token t = new Token()
        public string PlainLocal()
        {
            Token t = new Token();
            return t.Value;
        }
    }
}
