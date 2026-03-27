// Fixture for testing accesses_on with object initializer syntax.
// No SPO code — generic types only.
// Covers: object initializer { Prop = value }, multi-member, negative case.
namespace Sample
{
    public class Widget { public int Value; public string Name; }
    public class Gadget { public double Size; }

    public class Factory
    {
        // Regression guard: regular member access must still be found
        public void RegularAccess(Widget w)
        {
            int v = w.Value;
        }

        // Single-member object initializer — must be found
        public Widget SingleInit()
        {
            return new Widget { Value = 42 };
        }

        // Multi-member object initializer — both members must be found
        public Widget MultiInit()
        {
            return new Widget { Value = 1, Name = "hello" };
        }

        // Multi-line object initializer — each member on its own line
        public Widget MultiLine()
        {
            return new Widget
            {
                Value = 99,
                Name = "world",
            };
        }

        // Different type — must NOT appear in Widget results
        public Gadget OtherType()
        {
            return new Gadget { Size = 3.14 };
        }
    }
}
