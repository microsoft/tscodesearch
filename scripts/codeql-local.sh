#!/usr/bin/env bash
set -e

apt-get update -qq && apt-get install -qq -y curl unzip python3 git > /dev/null
curl -fsSL https://deb.nodesource.com/setup_20.x | bash - > /dev/null 2>&1
apt-get install -qq -y nodejs > /dev/null

REPO_URL="https://github.com/microsoft/tscodesearch.git"
REPO_BRANCH="main"
REPO_DIR=/tmp/repo
DB_DIR=/tmp/codeql-dbs
RESULTS_DIR=/tmp/codeql-results
CODEQL=/opt/codeql/codeql

mkdir -p "$DB_DIR" "$RESULTS_DIR"

echo "=== Downloading CodeQL CLI ==="
curl -sL https://github.com/github/codeql-cli-binaries/releases/latest/download/codeql-linux64.zip \
  -o /tmp/codeql.zip
unzip -q /tmp/codeql.zip -d /opt/
echo "CodeQL $($CODEQL version --format=terse)"

echo ""
echo "=== Cloning repo ==="
git clone --depth=1 --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
echo "Cloned. Files:"
find "$REPO_DIR" -not -path '*/.git/*' | wc -l

echo ""
echo "=== Creating Python database ==="
"$CODEQL" database create "$DB_DIR/python" \
  --language=python \
  --build-mode=none \
  --source-root="$REPO_DIR" \
  --codescanning-config="$REPO_DIR/.github/codeql/codeql-config.yml" \
  --overwrite

echo ""
echo "=== Creating JavaScript/TypeScript database ==="
"$CODEQL" database create "$DB_DIR/js" \
  --language=javascript \
  --build-mode=none \
  --source-root="$REPO_DIR" \
  --codescanning-config="$REPO_DIR/.github/codeql/codeql-config.yml" \
  --overwrite

echo ""
echo "=== Downloading query packs ==="
"$CODEQL" pack download codeql/python-queries codeql/javascript-queries

echo ""
echo "=== Analyzing Python ==="
"$CODEQL" database analyze "$DB_DIR/python" \
  codeql/python-queries:codeql-suites/python-code-quality.qls \
  --format=sarif-latest \
  --output="$RESULTS_DIR/python.sarif" \
  --sarif-add-snippets

echo ""
echo "=== Analyzing JavaScript/TypeScript ==="
"$CODEQL" database analyze "$DB_DIR/js" \
  codeql/javascript-queries:codeql-suites/javascript-code-quality.qls \
  --format=sarif-latest \
  --output="$RESULTS_DIR/js.sarif" \
  --sarif-add-snippets

echo ""
echo "=== Results summary ==="
python3 -c "
import json, os

for name in ['python', 'js']:
    path = '$RESULTS_DIR/' + name + '.sarif'
    if not os.path.exists(path):
        continue
    data = json.load(open(path))
    results = [r for run in data.get('runs', []) for r in run.get('results', [])]
    by_rule = {}
    for r in results:
        rule = r.get('ruleId', 'unknown')
        by_rule[rule] = by_rule.get(rule, 0) + 1
    print(f'\n{name.upper()} ({len(results)} total):')
    for rule, count in sorted(by_rule.items(), key=lambda x: -x[1]):
        print(f'  {count:3d}  {rule}')
"

echo ""
echo "=== Copying results to /repo ==="
cp "$RESULTS_DIR"/*.sarif /repo/codeql-results/ 2>/dev/null || true
echo "Done."
