// Fixture for testing recursive_pattern (property pattern matching).
// Bug: if (obj is Widget { Size: 0 } w) — uses recursive_pattern node, not
// declaration_pattern. The type and name fields exist but were not in the
// node-type set, so w was silently skipped.
// No SPO code — generic types only.
namespace Sample.Patterns
{
    public class Widget { public int Size { get; } public void Use() { } }
    public class Other  { public int Size { get; } public void Use() { } }

    public class Factory
    {
        // regression guard: plain declaration_pattern must still be found
        public void PlainPattern(object obj)
        {
            if (obj is Widget w)
                w.Use();
        }

        // the new form: recursive pattern with property clause + binding
        public void PropPattern(object obj)
        {
            if (obj is Widget { Size: 0 } wp)
                wp.Use();
        }

        // switch arm with recursive pattern
        public void SwitchPropPattern(object obj)
        {
            switch (obj)
            {
                case Widget { Size: > 0 } ws:
                    ws.Use();
                    break;
            }
        }

        // recursive pattern WITHOUT a binding name — must NOT produce a local
        public void NoBinding(object obj)
        {
            if (obj is Widget { Size: 0 }) { }
        }

        // negative: Other must NOT appear in Widget results
        public void OtherPropPattern(object obj)
        {
            if (obj is Other { Size: 1 } op)
                op.Use();
        }
    }
}
