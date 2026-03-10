// callers_fixture.cs — synthetic C# for caller-site unit tests
using System;
using System.Collections.Generic;
using System.Threading.Tasks;

namespace Test.Fixtures
{
    public interface IBlobStore
    {
        Task<IList<string>> GetBlobsAsync(string container, IList<string> blobs, object req);
        Task<string> GetBlobAsync(string id);
    }

    public class BlobService
    {
        private IBlobStore _store;
        private Task _task;

        // LINE 19 — direct awaited call — MATCH GetBlobsAsync
        public async Task<IList<string>> DirectCallAsync(string container)
        {
            return await _store.GetBlobsAsync(container, null, null);
        }

        // LINE 25 — ConfigureAwait pattern — MATCH GetBlobsAsync
        public async Task<IList<string>> ConfigureAwaitCallAsync(string container)
        {
            return await _store.GetBlobsAsync(container, null, null).ConfigureAwait(false);
        }

        // LINE 31 — .Result synchronous call — MATCH GetBlobsAsync
        public IList<string> SyncCall(string container)
        {
            return _store.GetBlobsAsync(container, null, null).Result;
        }

        // generic call — should match GetItemsAsync when that is the query
        public async Task GenericCallAsync()
        {
            var items = await _store.GetItemsAsync<string>("container");
        }

        // non-matching calls — should NOT appear for GetBlobsAsync
        public async Task NonMatchingAsync()
        {
            var awaiter = _task.GetAwaiter();          // GetAwaiter — no match
            var result  = awaiter.GetResult();          // GetResult  — no match
            var blob    = await _store.GetBlobAsync("id"); // GetBlobAsync (singular) — no match
            var x       = GetBlobsAsyncHelper("c");     // suffix via word boundary — no match
            string name = "GetBlobsAsync()";            // in string literal — regex WILL match (known text-only limitation)
        }

        private IList<string> GetBlobsAsyncHelper(string c) => null;
    }

    public class BlobServiceTests
    {
        private IBlobStore _mockStore;

        // LINE 57 — Moq Setup with lambda — MATCH GetBlobsAsync
        public void Setup_Returns_Blobs()
        {
            _mockStore.Setup(x => x.GetBlobsAsync(
                It.IsAny<string>(), null, null))
                .ReturnsAsync(new List<string>());
        }

        // LINE 65 — Moq Verify — MATCH GetBlobsAsync
        public void Verify_Called()
        {
            _mockStore.Verify(x => x.GetBlobsAsync(
                It.IsAny<string>(), null, null), Times.Once);
        }

        // LINE 72 — case-insensitive match — MATCH getblobsasync
        public void LowerCaseCall()
        {
            _mockStore.Setup(x => x.getblobsasync(null, null, null));
        }
    }
}
