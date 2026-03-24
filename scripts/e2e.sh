#!/usr/bin/env bash
# e2e.sh — runs INSIDE the container (via docker exec) or directly in WSL.
#
# Handles all inner work for the e2e test suite:
#   - Typesense health checks
#   - Collection readiness polling
#   - Running test_sample_e2e.py
#
# Config resolution (first match wins):
#   TS_PORT / TS_KEY   explicit env vars
#   ../config.json     auto-located relative to this script
#                        container: /app/scripts/e2e.sh → /app/config.json
#                        WSL repo:  .../tscodesearch/scripts/ → .../tscodesearch/config.json
#
# Commands:
#   health                              exit 0 if Typesense is healthy (single probe)
#   collection-count <coll>             print num_documents to stdout
#   wait-health [timeout=60]            poll until healthy; print progress to stderr
#   wait-collection <coll> [timeout=90] poll until >=1 doc; print progress to stderr
#   run-tests [pytest-args...]          run test_sample_e2e.py with CODESEARCH_TEST_DOCKER=1
#   run-suite <coll>... [-- pytest-args...]
#                                       wait-health + wait-collection(s) + run-tests
#
# Usage — Docker mode (from run_tests.mjs):
#   docker exec "$CONTAINER" /app/scripts/e2e.sh run-suite \
#       codesearch_root1 codesearch_root2 -- -v -k TestSample
#
# Usage — WSL native mode (from run_tests.mjs):
#   e2e_wait() { TS_PORT="$TS_PORT" TS_KEY="$TS_KEY" "$REPO/scripts/e2e.sh" "$@"; }
#   e2e_wait health           # single probe
#   e2e_wait wait-health 30   # startup wait loop
set -euo pipefail

# ── Resolve TS_PORT and TS_KEY from config.json if not already set ────────────

if [[ -z "${TS_PORT:-}" ]] || [[ -z "${TS_KEY:-}" ]]; then
    _SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    _CFG=""
    for _c in "${_SCRIPT_DIR}/../config.json"; do
        if [[ -f "$_c" ]]; then _CFG="$_c"; break; fi
    done
    if [[ -n "$_CFG" ]]; then
        : "${TS_PORT:=$(python3 -c "import json; print(json.load(open('$_CFG')).get('port',8108))" 2>/dev/null || echo 8108)}"
        : "${TS_KEY:=$(python3  -c "import json; print(json.load(open('$_CFG')).get('api_key',''))" 2>/dev/null || echo '')}"
    fi
fi

TS_PORT="${TS_PORT:-8108}"
TS_KEY="${TS_KEY:-}"
TS_URL="http://localhost:${TS_PORT}"
CURL="curl -sf --max-time 8"

# ── Helpers ───────────────────────────────────────────────────────────────────

_ts_healthy() {
    python3 "$(dirname "$0")/http_ok.py" "${TS_URL}/health" 2>/dev/null
}

_collection_count() {
    local coll="$1"
    local response
    response=$($CURL -H "X-TYPESENSE-API-KEY: ${TS_KEY}" "${TS_URL}/collections/${coll}" 2>/dev/null) \
        || { echo 0; return 0; }
    echo "$response" | python3 -c "import json,sys; print(json.load(sys.stdin).get('num_documents',0))" 2>/dev/null || echo 0
}

_wait_health() {
    local timeout="${1:-60}"
    echo -n "==> Waiting for Typesense health" >&2
    for i in $(seq 1 "$timeout"); do
        if _ts_healthy; then
            echo " OK (${i}s)" >&2; return 0
        fi
        if [[ $i -eq $timeout ]]; then
            echo "" >&2
            echo "[e2e] ERROR: Typesense not healthy after ${timeout}s" >&2
            echo "[e2e] Last response:" >&2
            $CURL "${TS_URL}/health" >&2 || true
            return 1
        fi
        echo -n "." >&2; sleep 1
    done
}

_wait_collection() {
    local coll="$1" timeout="${2:-90}"
    echo -n "==> Waiting for collection '$coll' to have documents" >&2
    for i in $(seq 1 "$timeout"); do
        local count
        count=$(_collection_count "$coll")
        if [[ "${count:-0}" -ge 1 ]]; then
            echo " OK ($count docs)" >&2; return 0
        fi
        if [[ $i -eq $timeout ]]; then
            echo "" >&2
            echo "[e2e] ERROR: '$coll' has no documents after ${timeout}s" >&2
            echo "[e2e] Collection response:" >&2
            $CURL -H "X-TYPESENSE-API-KEY: ${TS_KEY}" "${TS_URL}/collections/${coll}" >&2 || true
            return 1
        fi
        echo -n "." >&2; sleep 1
    done
}

# ── Commands ──────────────────────────────────────────────────────────────────

cmd="${1:-}"
case "$cmd" in
    health)
        _ts_healthy
        ;;

    collection-count)
        _collection_count "${2:?Usage: e2e.sh collection-count <collection>}"
        ;;

    wait-health)
        _wait_health "${2:-60}"
        ;;

    wait-collection)
        _wait_collection \
            "${2:?Usage: e2e.sh wait-collection <collection> [timeout]}" \
            "${3:-90}"
        ;;

    run-tests)
        shift
        CODESEARCH_TEST_DOCKER=1 python3 -m pytest /app/tests/ -v "$@"
        ;;

    run-suite)
        # run-suite <coll1> <coll2> ... [-- pytest-args...]
        shift
        colls=(); pytest_args=(); in_colls=true
        for arg in "$@"; do
            if [[ "$arg" == "--" ]]; then in_colls=false; continue; fi
            if $in_colls; then colls+=("$arg"); else pytest_args+=("$arg"); fi
        done

        _wait_health 60
        for coll in "${colls[@]+"${colls[@]}"}"; do
            _wait_collection "$coll" 90
        done

        echo "==> Running tests..." >&2
        CODESEARCH_TEST_DOCKER=1 python3 -m pytest /app/tests/ \
            -v "${pytest_args[@]+"${pytest_args[@]}"}"
        ;;

    *)
        echo "Usage: e2e.sh {health|collection-count|wait-health|wait-collection|run-tests|run-suite}" >&2
        exit 1
        ;;
esac
