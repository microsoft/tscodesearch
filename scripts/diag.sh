#!/bin/bash
# Diagnostics script — run inside the container:
#   docker exec codesearch /app/scripts/diag.sh

set -euo pipefail

CFG=/app/config.json
API_KEY=$(python3 -c "import json; print(json.load(open('$CFG'))['api_key'])" 2>/dev/null || echo "")
PORT=$(python3 -c "import json; print(json.load(open('$CFG')).get('port',8108))" 2>/dev/null || echo "8108")
API_PORT=$((PORT + 1))
TS_URL="http://localhost:${PORT}"
API_URL="http://localhost:${API_PORT}"
AUTH="-H X-TYPESENSE-API-KEY: ${API_KEY}"

echo "=== config.json ==="
cat "$CFG"
echo ""

echo "=== management API /status ==="
curl -sf -H "X-TYPESENSE-API-KEY: ${API_KEY}" "${API_URL}/status" | python3 -m json.tool 2>/dev/null || echo "(not reachable)"
echo ""

echo "=== management API /verify/status ==="
curl -sf -H "X-TYPESENSE-API-KEY: ${API_KEY}" "${API_URL}/verify/status" | python3 -m json.tool 2>/dev/null || echo "(no verify has run)"
echo ""

echo "=== Typesense collections ==="
curl -sf -H "X-TYPESENSE-API-KEY: ${API_KEY}" "${TS_URL}/collections" | python3 -m json.tool 2>/dev/null || echo "(not reachable)"
echo ""

echo "=== Source root paths ==="
python3 - <<'PYEOF'
import json, os
cfg = json.load(open('/app/config.json'))
for name, val in cfg.get('roots', {}).items():
    path = (val.get('local_path') or val.get('external_path', '')) if isinstance(val, dict) else str(val)
    exists = os.path.isdir(path) if path else False
    print(f"  [{name}] {path}  ({'OK' if exists else 'NOT FOUND'})")
PYEOF
echo ""

echo "=== Indexed sample (first 20 relative_paths) ==="
python3 - <<'PYEOF'
import json, urllib.request, os

cfg  = json.load(open('/app/config.json'))
key  = cfg.get('api_key', '')
port = cfg.get('port', 8108)
roots = cfg.get('roots', {})

for name, val in roots.items():
    coll = 'codesearch_' + __import__('re').sub(r'[^a-z0-9]', '_', name.lower())
    url  = f'http://localhost:{port}/collections/{coll}/documents/export?include_fields=relative_path&limit=20'
    try:
        req = urllib.request.Request(url, headers={'X-TYPESENSE-API-KEY': key})
        resp = urllib.request.urlopen(req, timeout=10).read().decode()
        paths = [json.loads(l).get('relative_path','') for l in resp.splitlines() if l.strip()]
        print(f"  [{name}] ({coll}) — {len(paths)} sampled:")
        for p in paths:
            print(f"    {p}")
    except Exception as e:
        print(f"  [{name}] export error: {e}")
PYEOF
echo ""

echo "=== Logs (last 30 lines each) ==="
for f in /typesensedata/api.log /typesensedata/typesense.log; do
    if [ -f "$f" ]; then
        echo "--- $f ---"
        tail -30 "$f"
        echo ""
    fi
done

# Check for indexer logs
for f in /typesensedata/indexer_*.log; do
    if [ -f "$f" ]; then
        echo "--- $f ---"
        tail -20 "$f"
        echo ""
    fi
done
