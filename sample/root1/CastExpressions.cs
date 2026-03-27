// Fixture for testing uses_kind=cast with C# cast forms.
// No SPO code — generic types only.
// Covers: explicit cast (Type)expr, as-expression obj as Type.
namespace Sample
{
    public class Animal { }
    public class Dog : Animal { public void Bark() { } }
    public class Cat : Animal { public void Meow() { } }

    public class Shelter
    {
        // Regression guard: explicit C-style cast — must still be found
        public void ExplicitCast(Animal a)
        {
            Dog d = (Dog)a;
            d.Bark();
        }

        // as-expression — must be found (Round 13 bug)
        public void AsCast(Animal a)
        {
            Dog d = a as Dog;
            if (d != null) d.Bark();
        }

        // as-expression with null check pattern
        public void AsCastNested(object obj)
        {
            Dog dog = obj as Dog;
            dog?.Bark();
        }

        // different type — must NOT appear in Dog results
        public void OtherType(Animal a)
        {
            Cat c = a as Cat;
            c?.Meow();
        }
    }
}
