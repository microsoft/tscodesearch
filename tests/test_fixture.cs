using System;
using System.Collections.Generic;

namespace TestFixture
{
    public interface IRepository<T>
    {
        T GetById(int id);
        IEnumerable<T> GetAll();
        void Save(T item);
        void Delete(int id);
    }

    public class Item
    {
        public int Id { get; set; }
        public string Name { get; set; }
    }

    [Serializable]
    public class Repository : IRepository<Item>
    {
        private readonly List<Item> _items;
        private IRepository<Item> _backing;

        public Repository(List<Item> items)
        {
            _items = items;
        }

        public Item GetById(int id)
        {
            return _items.Find(x => x.Id == id);
        }

        public IEnumerable<Item> GetAll()
        {
            return _items;
        }

        public void Save(Item item)
        {
            _items.Add(item);
        }

        public void Delete(int id)
        {
            _items.RemoveAll(x => x.Id == id);
        }
    }

    public class CachedRepository : IRepository<Item>
    {
        private readonly Repository _inner;
        private readonly Dictionary<int, Item> _cache;

        public CachedRepository(Repository inner)
        {
            _inner = inner;
            _cache = new Dictionary<int, Item>();
        }

        public Item GetById(int id)
        {
            if (_cache.TryGetValue(id, out var item))
                return item;
            var result = _inner.GetById(id);
            _cache[id] = result;
            return result;
        }

        public IEnumerable<Item> GetAll() => _inner.GetAll();

        public void Save(Item item)
        {
            _inner.Save(item);
            _cache[item.Id] = item;
        }

        public void Delete(int id)
        {
            _inner.Delete(id);
            _cache.Remove(id);
        }
    }
}
