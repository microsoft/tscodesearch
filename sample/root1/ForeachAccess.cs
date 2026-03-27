// Fixture for testing accesses_on with foreach iteration variables.
// No SPO code — generic types only.
using System.Collections.Generic;

namespace Sample
{
    public class Item
    {
        public string Name;
        public int Count;
    }

    public class Processor
    {
        // Explicit-type foreach: iteration variable typed as Item
        public void ProcessAll(List<Item> items)
        {
            foreach (Item item in items)
            {
                Log(item.Name);
                Total += item.Count;
            }
        }

        // var-inferred foreach: type cannot be resolved without inference
        public void ProcessVar(List<Item> items)
        {
            foreach (var entry in items)
            {
                Log(entry.Name);
            }
        }

        // Nested foreach: both typed
        public void ProcessNested(List<Item> outer, List<Item> inner)
        {
            foreach (Item a in outer)
            {
                foreach (Item b in inner)
                {
                    Log(a.Name + b.Name);
                }
            }
        }

        private int Total;
        private void Log(string s) { }
    }
}
