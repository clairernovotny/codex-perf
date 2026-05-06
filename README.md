# codex-perf

Local tools for making a large Codex Desktop profile responsive.

The short version: this repo repairs oversized local thread-title metadata, then
starts `Codex.app` through a Chrome DevTools Protocol wrapper that injects a
renderer-side fast path for large thread navigation. The user-facing goal is
simple: Codex should feel like Codex, just faster.

## Overview

The project has two parts.

First, `scripts/fix-codex-perf.py` keeps thread titles bounded. Codex stores
thread metadata in `~/.codex/state_5.sqlite`, rollout JSONL files, and
`session_index.jsonl`. The repair tool backs up those files, validates them, and
writes consistent short titles plus later `ThreadNameUpdated` events so future
reconciliation keeps the same title.

Second, `scripts/codex-perf-launch.py` starts `Codex.app` with CDP on localhost
port `17373` and injects `renderer/fast-thread-loader.js`. The renderer patch
intercepts large-thread activation and shows conversation content from preloaded
local rollout pages, with older turns loaded automatically in the background.

This repo has one tool for each path:

| Path | Tool | What it changes |
| --- | --- | --- |
| Thread-title metadata | `scripts/fix-codex-perf.py` | Repairs local Codex metadata after taking a backup |
| Large-thread navigation | `scripts/codex-perf-launch.py` + `renderer/fast-thread-loader.js` | Injects a renderer-only fast thread surface through CDP |

The wrapper is the launch path. It starts or attaches to `Codex.app` with CDP on
localhost, using port `17373` by default.

## Quick Start

Create a read-only backup of your Codex state:

```bash
python3 scripts/fix-codex-perf.py backup
```

Run the repair against a copied Codex home first:

```bash
VERIFY_ROOT=/tmp/codex-perf-verify-$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$VERIFY_ROOT/codex-home"
rsync -a "$HOME/.codex/" "$VERIFY_ROOT/codex-home/.codex/"

python3 scripts/fix-codex-perf.py \
  --codex-home "$VERIFY_ROOT/codex-home/.codex" \
  repair \
  --backup-dir "$VERIFY_ROOT/backups"
```

Launch Codex through the wrapper:

```bash
python3 scripts/codex-perf-launch.py
```

Attach to an already CDP-enabled Codex process:

```bash
python3 scripts/codex-perf-launch.py --no-launch
```

## How It Works

```text
~/.codex/state_5.sqlite + sessions/*.jsonl + session_index.jsonl
       |
       |  scripts/fix-codex-perf.py
       |  backup first, validate first, then repair titles
       v
bounded thread titles + later ThreadNameUpdated events

Codex.app --remote-debugging-port=17373
       |
       |  scripts/codex-perf-launch.py
       |  attach through CDP and inject renderer JS
       v
renderer/fast-thread-loader.js
       |
       |  intercept large thread activation
       |  render conversation content from preloaded local rollout pages
       v
fast thread surface that should look and behave like normal Codex
```

The repair tool and the wrapper deliberately have different responsibilities.

`scripts/fix-codex-perf.py` owns Codex metadata repair. It is Python
standard-library only and supports `backup`, `repair`, `restore`, and `measure`.
Mutating commands create a timestamped backup package first, validate SQLite and
JSONL inputs before writing, preserve `first_user_message`, and update the title
sources used by local reconciliation.

`scripts/codex-perf-launch.py` starts or attaches to `Codex.app` with CDP. It
uses local CDP port `17373` by default, starts a loopback thread-data server, and
embeds recent thread pages into the injected script. The embedded pages are the
current fast path.

`renderer/fast-thread-loader.js` installs the renderer-side behavior. It has a
localStorage kill switch at `codex-perf-fast-thread-loader:disabled`, can clean
up its listeners, timers, styles, and patches through `stop()`, and emits
measurement events for navigation start, first paint, navigation end, and
background older-turn loading.

## Commands

### Metadata Tool

```bash
python3 scripts/fix-codex-perf.py --help
```

| Command | Purpose |
| --- | --- |
| `backup` | Create a timestamped read-only backup package |
| `repair` | Back up, repair title metadata, append reconciliation events, and write metrics |
| `restore --backup <path>` | Restore files from a selected backup manifest and validate hashes |
| `measure --phase title` | Write title/query/list measurement artifacts |
| `measure --phase thread-loading` | Write thread-loading measurement artifacts where available |
| `measure --phase all` | Run all measurement phases |

Examples:

```bash
python3 scripts/fix-codex-perf.py repair
python3 scripts/fix-codex-perf.py restore --backup ~/.codex/backups/thread-perf-fix-YYYYMMDD-HHMMSS -y
python3 scripts/fix-codex-perf.py measure --phase all
```

When using the live default `~/.codex`, mutating commands check for running Codex
Desktop, Codex CLI, and Codex app-server processes. The default prompt is to
exit before mutation.

### CDP Wrapper

```bash
python3 scripts/codex-perf-launch.py --help
```

Useful options:

| Option | Purpose |
| --- | --- |
| `--port 17373` | Optional local CDP port override. Defaults to `17373`; use `0` to pick a free port |
| `--app-path /Applications/Codex.app` | Codex Desktop app bundle path |
| `--workspace <path>` | Workspace to open |
| `--codex-home <path>` | Codex home used for local thread data |
| `--renderer-js renderer/fast-thread-loader.js` | Renderer script to inject |
| `--output-dir <path>` | Directory for CDP metrics artifacts |
| `--no-launch` | Attach to an existing CDP-enabled app |
| `--no-inject` | Collect a comparison measurement with renderer injection disabled |
| `--no-measure` | Launch and inject only |
| `--preload-thread-count <n>` | Number of recent active threads to embed |
| `--preload-turn-count <n>` | Maximum newest turns to embed per thread |
| `--preload-text-chars <n>` | Maximum characters per rendered turn |

## Safety Model

- The repair tool preserves chat history.
- `first_user_message` is preserved.
- Rollout JSONL repair is append-only.
- `session_index.jsonl` is updated so local reconciliation keeps the repaired
  title.
- Every mutating repair/restore path creates or consumes a manifest with file
  hashes.
- Restore validates the selected backup and confirms SQLite integrity after
  replacement.
- The CDP wrapper is renderer-only.
- The renderer patch has a kill switch and a cleanup path.

## Proof From The Current Run

The final copied-current verification repaired a copied profile:

| Check | Result |
| --- | --- |
| Copied-current home | `/tmp/codex-perf-completion-20260506-005012/codex-home/.codex` |
| Repaired rows | `93` |
| Rollout title events appended | `93` |
| Session-index events appended | `93` |
| SQLite integrity | `ok` |
| Active titles over 120 chars after repair | `0` |
| Affected rows still using full first message as title | `0` |
| Idempotence second repair | `repaired=0` |
| Restore hash check | `all_match=true`, `checked=95` |

Renderer measurement with the wrapper injected:

| Metric | Value |
| --- | --- |
| First visible content | `2.3000000715255737 ms` |
| Settled shell | `9.300000071525574 ms` |
| Long task count | `0` |
| Total long task duration | `0 ms` |
| Max long task duration | `0 ms` |
| Heap before / after / post-GC | `210379780 / 210479760 / 201477448` bytes |
| Transient heap spike | `99980` bytes |
| Older turns loaded automatically | `true` |
| Rendered article count | `50` |

## Current Scope

- Metadata repair is a cross-platform Python tool.
- The launcher targets macOS `Codex.app`.
- The fast thread surface uses preloaded local rollout pages.
- Live repair of the default `~/.codex` runs after Codex processes are stopped
  or explicitly terminated by the repair command.
- Copied-home verification is the recommended proof path before live repair.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `CDP target list unavailable` | Launch Codex with the same `--port`, or rerun the wrapper launch command |
| Wrapper attaches and the native path stays active | Clear localStorage key `codex-perf-fast-thread-loader:disabled` |
| Repair exits before mutation | Stop Codex Desktop/CLI/app-server, or rerun with `-y` if terminating matching processes is acceptable |
| Restore fails hash validation | Use the backup directory containing the original `manifest.json`; keep backup files unchanged |
| Selected thread is missing from the fast path | Increase `--preload-thread-count` or `--preload-turn-count`, then relaunch the wrapper |

## Development

Run the tests:

```bash
python3 -m unittest discover -s tests -v
```

Check Python and renderer syntax:

```bash
python3 -m py_compile scripts/fix-codex-perf.py scripts/codex-perf-launch.py
node --check renderer/fast-thread-loader.js
```

Before publishing a change that touches naming or launch behavior, audit the
repository and history for the current wrapper terminology.
