# Agent notes

Operational notes for Claude / future agents working in this repo. For
architecture, module map, and conventions see `CLAUDE.md`.

## Debugging the indexer: real-time CSV logs

When something goes wrong on the indexer side (the classic symptom: files
get re-indexed on every restart even though the index already contains
them), turn on the CSV debug log.

### Enable

Set `csv_debug` in `config.json`:

```json
{
  "port": 8108,
  "csv_debug": true,
  "roots": { "default": { "path": "q:/spocore/src" } }
}
```

Then `node ts.mjs restart` (or `ts restart` if the cmd is on PATH).

Accepted values:

| value                              | effect                                                          |
|------------------------------------|-----------------------------------------------------------------|
| `false` / `""` / unset             | off                                                             |
| `true` / `"1"` / `"on"` / `"true"` | logs to `%LOCALAPPDATA%/tscodesearch/csv/`                      |
| any other string                   | treated as an explicit directory path -- logs land there        |

Logging is opt-in and append-only; rows from successive restarts share a
file and are distinguished by the per-row `pid` column.

### CSV files

Written by `indexserver/csv_log.py`, one file per event type. Each row
starts with `ts` (ms-precision local time) and `pid`.

| file                 | rows are written when                                              | useful columns                                      |
|----------------------|--------------------------------------------------------------------|------------------------------------------------------|
| `session.csv`        | daemon start/stop                                                  | `action` = `start`/`stop`                            |
| `backend_export.csv` | verifier exports the index map at scan start (once per session)    | `doc_id`, `mtime`, `relative_path`                   |
| `fs_walk.csv`        | every file the verifier walks against the exported map             | `decision` = `matched` / `missing` / `stale`         |
| `orphan.csv`         | index entries with no matching file on disk (about to be deleted)  | `doc_id`                                             |
| `enqueue.csv`        | every `IndexQueue.enqueue` (including dedup hits)                  | `action`, `reason`, `is_new`                         |
| `parse.csv`          | every tree-sitter parse the queue worker runs                      | `parse_ms`, `ok`, `error`                            |
| `commit.csv`         | every Tantivy commit, success or failure                           | `duration_ms`, `success`, `error`                    |
| `watcher.csv`        | every watchdog event                                               | `src_path`, `action`                                 |

Decision values in `fs_walk.csv`:

* `matched` -- file's mtime equals the indexed mtime; nothing to do
* `missing` -- file's `doc_id` is not in the index; enqueued as `new`
* `stale`   -- file is in the index but its mtime has changed; enqueued as `modified`

### Scanning the logs

`scripts/scan_csv.py` ingests the CSV directory and prints per-session
breakdowns plus anomaly flags.

```powershell
# default: summary table (one row per restart, anomaly notes on the right)
.client-venv/Scripts/python.exe -m scripts.scan_csv

# list each session with timestamps and counts
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode sessions

# drill into one session
.client-venv/Scripts/python.exe -m scripts.scan_csv --session 1 --mode summary

# top stale files with mtime-delta buckets (was the change recent? batched?)
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode stales

# list missing files (filename + on-disk mtime)
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode missing

# orphan doc ids with relative_path (joined from the same session's export)
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode orphans

# every commit / parse failure with the error string
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode errors

# diff the last two index exports: what disappeared, what appeared, what changed mtime
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode index-diff

# files that flipped between matched and stale/missing across sessions
.client-venv/Scripts/python.exe -m scripts.scan_csv --mode flapping
```

Override the log directory with `--csv-dir DIR` or
`TSCODESEARCH_CSV_DIR=DIR`.

### Anomaly flags emitted by `summary`

* `INDEX_EMPTY_ON_OPEN` -- the index reported 0 docs at scan start; if
  the previous session ended with a populated index this means the
  Tantivy state was lost between restarts (e.g. `meta.json` blown away,
  segments unreachable). Look for prior `commit.csv` failures.
* `ALL_MISSING` -- every walked file is missing from the index. Same
  signal as the empty-index case but observed via the fs walk.
* `COMMIT_FAIL=N` -- Tantivy commit failed N times. The error column in
  `commit.csv` shows the cause (typically Windows `Access is denied`).
* `ORPHAN_HEAVY (X/Y)` -- more than half the index entries didn't match
  any file on disk; check whether the configured root path changed.
* `PARSE_FAIL=N` -- file read / tree-sitter parse failures.

### Disabling

Set `"csv_debug": false` in `config.json` and restart. The files stick
around for offline analysis; delete the `csv/` directory when done.
