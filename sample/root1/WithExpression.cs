// Fixture for testing accesses_on with C# 9 with-expression record mutation.
// No SPO code — generic types only.
// Covers: with { single }, with { multiple }, multi-line with, negative case.
namespace Sample
{
    public record Coord(int X, int Y, int Z);
    public record Color(int R, int G, int B);

    public class Transformer
    {
        // Regression guard: regular member access must still be found
        public void RegularAccess(Coord c)
        {
            int x = c.X;
        }

        // Single-member with expression
        public Coord ShiftX(Coord c)
        {
            return c with { X = 0 };
        }

        // Multi-member with expression — both members on same line
        public Coord Reset(Coord c)
        {
            return c with { X = 0, Y = 0 };
        }

        // Multi-line with expression — each member on its own line
        public Coord FullReset(Coord c)
        {
            return c with
            {
                X = 0,
                Y = 0,
                Z = 0,
            };
        }

        // Different type — must NOT appear in Coord results
        public Color Darken(Color col)
        {
            return col with { R = 0, G = 0 };
        }
    }
}
