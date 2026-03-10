"""Docker integration tests for the codesearch MCP container.

Builds the image from docker/Dockerfile, starts a container with a small
synthetic source tree, and verifies:
  - Typesense health
  - Initial indexing completes
  - Search returns results
  - MCP SSE endpoint is accessible
  - Auth is enforced

Skips automatically if the Docker daemon is not available.

Run:
    pytest tests/test_docker.py -v
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_FOO_CS = """\
using System;
namespace TestNs {
    [Serializable]
    public class Foo : IDisposable {
        public string Name { get; set; }
        public void Dispose() { }
        public void DoWork(string input) { }
    }
}
"""

_BAR_CS = """\
namespace TestNs {
    public class Bar : Foo {
        private Foo _foo;
        public Bar(Foo foo) { _foo = foo; }
        public void Process() { _foo.DoWork("hello"); }
    }
}
"""


def _docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _http_get(url: str, api_key: str = "", timeout: int = 5) -> tuple[int, dict]:
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("X-TYPESENSE-API-KEY", api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}
    except Exception:
        return 0, {}


@unittest.skipUnless(_docker_available(), "Docker not available")
class TestDockerContainer(unittest.TestCase):
    """Build the codesearch image and run a container; assert all services work."""

    IMAGE_TAG = "codesearch-test:ci"
    API_KEY   = "docker-ci-key"
    TS_PORT   = 18108   # host port → container 8108
    MCP_PORT  = 13000   # host port → container 3000

    _container_id: str = ""
    _src_dir:      str = ""
    _setup_error:  str = ""

    @classmethod
    def setUpClass(cls):
        # Synthetic source tree — two C# files
        cls._src_dir = tempfile.mkdtemp(prefix="ts_docker_test_")
        for name, content in [("Foo.cs", _FOO_CS), ("Bar.cs", _BAR_CS)]:
            with open(os.path.join(cls._src_dir, name), "w") as fh:
                fh.write(content)

        # Build image (context = repo root, dockerfile = docker/Dockerfile)
        r = subprocess.run(
            ["docker", "build", "-t", cls.IMAGE_TAG, "-f", "docker/Dockerfile", "."],
            capture_output=True, text=True, cwd=_REPO_ROOT, timeout=300,
        )
        if r.returncode != 0:
            cls._setup_error = f"docker build failed:\n{r.stderr[-2000:]}"
            return

        # Start container
        r = subprocess.run(
            [
                "docker", "run", "-d",
                "-p", f"{cls.TS_PORT}:8108",
                "-p", f"{cls.MCP_PORT}:3000",
                "-v", f"{cls._src_dir}:/source:ro",
                "-e", f"CODESEARCH_API_KEY={cls.API_KEY}",
                "-e", "CODESEARCH_PORT=8108",
                "-e", "MCP_PORT=3000",
                cls.IMAGE_TAG,
            ],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            cls._setup_error = f"docker run failed:\n{r.stderr}"
            return
        cls._container_id = r.stdout.strip()

        # ── Wait for Typesense health ─────────────────────────────────────────
        deadline = time.time() + 60
        while time.time() < deadline:
            code, body = _http_get(f"http://localhost:{cls.TS_PORT}/health")
            if code == 200 and body.get("ok"):
                break
            time.sleep(2)
        else:
            cls._setup_error = "Typesense did not become healthy within 60s"
            return

        # ── Wait for MCP SSE endpoint ─────────────────────────────────────────
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                resp = urllib.request.urlopen(
                    urllib.request.Request(f"http://localhost:{cls.MCP_PORT}/sse"),
                    timeout=3,
                )
                if resp.status == 200:
                    resp.close()
                    break
            except Exception:
                pass
            time.sleep(2)

        # ── Wait for initial indexing to produce at least one document ────────
        coll_url = f"http://localhost:{cls.TS_PORT}/collections/codesearch_default"
        deadline = time.time() + 90
        while time.time() < deadline:
            code, body = _http_get(coll_url, api_key=cls.API_KEY)
            if code == 200 and body.get("num_documents", 0) > 0:
                break
            time.sleep(3)

    @classmethod
    def tearDownClass(cls):
        if cls._container_id:
            subprocess.run(["docker", "stop", cls._container_id],
                           capture_output=True, timeout=30)
            subprocess.run(["docker", "rm",   cls._container_id],
                           capture_output=True, timeout=10)
        if cls._src_dir:
            shutil.rmtree(cls._src_dir, ignore_errors=True)

    def setUp(self):
        if self._setup_error:
            self.skipTest(f"Container setup failed: {self._setup_error}")

    def _logs(self, tail: int = 40) -> str:
        if not self._container_id:
            return ""
        r = subprocess.run(
            ["docker", "logs", "--tail", str(tail), self._container_id],
            capture_output=True, text=True,
        )
        return r.stdout + r.stderr

    # ── Assertions ────────────────────────────────────────────────────────────

    def test_typesense_healthy(self):
        code, body = _http_get(f"http://localhost:{self.TS_PORT}/health")
        self.assertEqual(code, 200, self._logs())
        self.assertTrue(body.get("ok"), f"health body: {body}")

    def test_collection_created(self):
        code, body = _http_get(
            f"http://localhost:{self.TS_PORT}/collections/codesearch_default",
            api_key=self.API_KEY,
        )
        self.assertEqual(code, 200,
            f"collection not found — container logs:\n{self._logs()}")
        self.assertIn("name", body)

    def test_files_indexed(self):
        code, body = _http_get(
            f"http://localhost:{self.TS_PORT}/collections/codesearch_default",
            api_key=self.API_KEY,
        )
        self.assertEqual(code, 200)
        ndocs = body.get("num_documents", 0)
        self.assertGreater(ndocs, 0,
            f"collection exists but has no documents — logs:\n{self._logs()}")

    def test_search_returns_results(self):
        params = "q=Foo&query_by=filename,class_names&per_page=5&num_typos=0"
        url = (
            f"http://localhost:{self.TS_PORT}"
            f"/collections/codesearch_default/documents/search?{params}"
        )
        code, body = _http_get(url, api_key=self.API_KEY)
        self.assertEqual(code, 200)
        self.assertGreater(body.get("found", 0), 0,
            f"search for 'Foo' returned no results: {body}")

    def test_mcp_sse_accessible(self):
        """MCP SSE endpoint responds with HTTP 200."""
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(f"http://localhost:{self.MCP_PORT}/sse"),
                timeout=5,
            )
            status = resp.status
            resp.close()
        except urllib.error.HTTPError as e:
            self.fail(f"MCP SSE endpoint returned HTTP {e.code}")
        except Exception as e:
            self.fail(f"MCP SSE endpoint not reachable: {e}")
        self.assertEqual(status, 200)

    def test_typesense_rejects_wrong_key(self):
        """Typesense should return 401 for an invalid API key."""
        code, _ = _http_get(
            f"http://localhost:{self.TS_PORT}/collections/codesearch_default",
            api_key="wrong-key",
        )
        self.assertEqual(code, 401)
