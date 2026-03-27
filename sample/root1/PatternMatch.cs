// Fixture for testing accesses_on with C# pattern matching.
// No SPO code — generic types only.
// Covers: is-pattern (if), switch-case pattern, negated is, combined && condition.
namespace Sample
{
    public class Shape { public double Area; public string Color; }
    public class Circle : Shape { public double Radius; }
    public class Rectangle : Shape { public double Width; public double Height; }

    public class Renderer
    {
        // if-is pattern: binds 'c' as Circle
        public void DrawShape(Shape s)
        {
            if (s is Circle c)
            {
                Render(c.Radius);
                Log(c.Color);
            }
        }

        // switch-case pattern: binds 'r' as Rectangle
        public double Measure(Shape s)
        {
            switch (s)
            {
                case Rectangle r:
                    return r.Width * r.Height;
                case Circle ci:
                    return ci.Radius * ci.Radius * 3.14;
                default:
                    return 0;
            }
        }

        // combined condition: if (x is Widget w && w.Active)
        public void Conditional(object obj)
        {
            if (obj is Circle combo && combo.Radius > 0)
            {
                Log(combo.Color);
            }
        }

        // plain local variable — should still be found
        public void PlainLocal()
        {
            Circle local = new Circle();
            Log(local.Color);
        }

        private void Render(double v) { }
        private void Log(object v) { }
    }
}
