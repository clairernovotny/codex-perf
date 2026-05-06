# codex-perf

Local tools for making a slow Codex Desktop profile usable again without patching
the app bundle.

The short version: this repo fixes bad local metadata that makes thread lists
heavy, then starts `Codex.app` through a Chrome DevTools Protocol wrapper that
injects a renderer-only fast path for large thread navigation. The user-facing
goal is simple: Codex should feel like Codex, just faster.

## The Story

There were two separate performance problems hiding behind the same symptom.

First, some local `threads.title` values had fallen back to huge
`first_user_message` payloads. That makes sidebar and thread-list work expensive:
SQLite returns bigger rows, JSON encoding gets bigger, and every list refresh
carries data that should have been a short label.

Second, after title metadata is repaired, opening a very large thread can still
stall the renderer. The slow part is no longer the title query. It is the native
thread view hydrating and reconciling a lot of local conversation history at
once.

This repo handles those separately:

| Problem | Tool | What it changes |
| --- | --- | --- |
| Bloated thread titles | `scripts/fix-codex-perf.py` | Repairs local Codex metadata, after taking a backup |
| Large-thread renderer stalls | `scripts/codex-perf-launch.py` + `renderer/fast-thread-loader.js` | Injects a renderer-only fast thread surface through CDP |

The old app-bundle hook approach is gone. The wrapper launches or attaches to
`Codex.app` with CDP on localhost, using port `17373` by default.

## Quick Start

Back up your Codex state without mutating anything:

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
python3 scripts/codex-perf-launch.py --port 17373
```

Attach to an already CDP-enabled Codex process:

```bash
python3 scripts/codex-perf-launch.py --no-launch --port 17373
```

Disable injection and measure stock behavior:

```bash
python3 scripts/codex-perf-launch.py --no-launch --no-inject --port 17373
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

`scripts/fix-codex-perf.py` is the only code that mutates Codex state. It is
Python standard-library only and supports `backup`, `repair`, `restore`, and
`measure`. Mutating commands create a timestamped backup package first, validate
SQLite and JSONL inputs before writing, preserve `first_user_message`, and update
all title sources that would otherwise reintroduce the bad label during
reconciliation.

`scripts/codex-perf-launch.py` starts or attaches to `Codex.app` with CDP. It
does not edit `state_5.sqlite`, rollout JSONL, or `session_index.jsonl`. By
default it uses local CDP port `17373`, starts a loopback thread-data server, and
embeds recent thread pages into the injected script. The embedded pages are the
current working fast path because browser fetches from the app scheme to the
loopback server are blocked.

`renderer/fast-thread-loader.js` installs the renderer-side behavior. It fails
open on unknown app shapes, has a localStorage kill switch at
`codex-perf-fast-thread-loader:disabled`, can clean up its listeners, timers,
styles, and patches through `stop()`, and emits measurement events for navigation
start, first paint, navigation end, and background older-turn loading.

## Commands

### Metadata Tool

```bash
python3 scripts/fix-codex-perf.py --help
```

| Command | Purpose |
| --- | --- |
| `backup` | Create a timestamped backup package without changing Codex state |
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
| `--port 17373` | Local CDP port. Use `0` to pick a free port |
| `--app-path /Applications/Codex.app` | Codex Desktop app bundle path |
| `--workspace <path>` | Workspace to open |
| `--codex-home <path>` | Codex home used for local thread data |
| `--renderer-js renderer/fast-thread-loader.js` | Renderer script to inject |
| `--output-dir <path>` | Directory for CDP metrics artifacts |
| `--no-launch` | Attach to an existing CDP-enabled app |
| `--no-inject` | Measure or run without the fast path |
| `--no-measure` | Launch and inject without collecting measurements |
| `--preload-thread-count <n>` | Number of recent active threads to embed |
| `--preload-turn-count <n>` | Maximum newest turns to embed per thread |
| `--preload-text-chars <n>` | Maximum characters per rendered turn |

## Safety Model

- The repair tool never truncates or deletes chat history.
- `first_user_message` is preserved.
- Rollout JSONL repair is append-only.
- `session_index.jsonl` is updated so local reconciliation keeps the repaired
  title.
- Every mutating repair/restore path creates or consumes a manifest with file
  hashes.
- Restore validates the selected backup and confirms SQLite integrity after
  replacement.
- The CDP wrapper never writes Codex metadata files.
- The renderer patch has a kill switch and a cleanup path.

## Proof From The Current Run

The final copied-current verification repaired the state without touching the
live default profile:

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

Stock-mode check with `--no-inject` restored the slow behavior:

| Metric | Value |
| --- | --- |
| Injection still active | `false` |
| Max long task duration | `1908 ms` |
| Total long task duration | `33318 ms` |

## Limitations

- This is a local workaround, not an upstream Codex patch.
- The wrapper is currently implemented for macOS `Codex.app`.
- The repair tool is cross-platform Python, but the CDP launcher assumes the
  macOS app bundle path and launch behavior.
- The fast thread surface currently uses preloaded local rollout pages. It is
  intended to feel like the normal thread view, but it is not yet a complete
  replacement for every native thread interaction.
- The loopback thread-data server exists, but the app scheme currently blocks
  browser fetches to it. Preloading is the working data path.
- Live repair of the default `~/.codex` should only happen when Codex processes
  are stopped or explicitly allowed to be terminated. The copied-home workflow is
  the safer verification path.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| `CDP target list unavailable` | Make sure Codex was launched with the same `--port`, or rerun without `--no-launch` |
| Wrapper attaches but nothing changes | Check localStorage for `codex-perf-fast-thread-loader:disabled=1` and clear it |
| Repair exits before mutation | Stop Codex Desktop/CLI/app-server, or rerun with `-y` if terminating matching processes is acceptable |
| Restore fails hash validation | Use the backup directory containing the original `manifest.json`; do not edit files inside the backup |
| Fast path does not have the thread | Increase `--preload-thread-count` or `--preload-turn-count`, then relaunch the wrapper |

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
repository and history for stale app-bundle-hook terminology.

## About Contributions

Please don't take this the wrong way, but I do not accept outside contributions
for any of my projects. I simply don't have the mental bandwidth to review
anything, and it's my name on the thing, so I'm responsible for any problems it
causes; thus, the risk-reward is highly asymmetric from my perspective. I'd also
have to worry about other "stakeholders," which seems unwise for tools I mostly
make for myself for free. Feel free to submit issues, and even PRs if you want
to illustrate a proposed fix, but know I won't merge them directly. Instead,
I'll have Claude or Codex review submissions via `gh` and independently decide
whether and how to address them. Bug reports in particular are welcome. Sorry if
this offends, but I want to avoid wasted time and hurt feelings. I understand
this isn't in sync with the prevailing open-source ethos that seeks community
contributions, but it's the only way I can move at this velocity and keep my
sanity.
