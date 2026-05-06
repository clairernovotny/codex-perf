#!/usr/bin/env python3
"""Backup, repair, restore, and measure Codex thread title metadata.

The tool is deliberately standalone and standard-library only.  State repair is
kept here; the packaged CDP wrapper renderer patch never writes Codex metadata files.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import select
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


TOOL_VERSION = "0.1.0"
TITLE_MAX_LEN = 120
MANIFEST_NAME = "manifest.json"
THREAD_EVENT_TYPE = "ThreadNameUpdated"


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str
    command: str


@dataclass
class ThreadRow:
    id: str
    rollout_path: Path
    title: str
    first_user_message: str
    source: str
    cwd: str
    updated_at_ms: int | None


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_z(value: dt.datetime | None = None) -> str:
    value = value or utc_now()
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp_slug() -> str:
    return utc_now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max(max_len - 3, 0)] + "..."


def summarize_for_label(text: str) -> str:
    first_line = text.split("\n", 1)[0]
    if first_line.endswith("\r"):
        first_line = first_line[:-1]
    first_line = first_line.strip()
    return truncate(first_line, TITLE_MAX_LEN)


def default_codex_home() -> Path:
    return Path.home() / ".codex"


def resolve_codex_home(value: str | None) -> Path:
    return (Path(value).expanduser() if value else default_codex_home()).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(original: Path, backup: Path) -> dict[str, Any]:
    return {
        "original_path": str(original),
        "backup_path": str(backup),
        "sha256": sha256_file(original),
        "size": original.stat().st_size,
    }


def backup_destination(backup_root: Path, original: Path, codex_home: Path) -> Path:
    try:
        rel = original.resolve().relative_to(codex_home.resolve())
        return backup_root / "files" / rel
    except ValueError:
        digest = hashlib.sha256(str(original.resolve()).encode("utf-8")).hexdigest()[:16]
        return backup_root / "external" / digest / original.name


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(text)
        tmp = Path(handle.name)
    os.replace(tmp, path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{line_number}: JSONL entry is not an object")
            rows.append(obj)
    return rows


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def sqlite_connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA busy_timeout=10000")
    return con


def integrity_check(db_path: Path) -> str:
    con = sqlite_connect(db_path)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        return str(row[0] if row else "")
    finally:
        con.close()


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def active_where(columns: set[str]) -> str:
    clauses = []
    if "archived" in columns:
        clauses.append("COALESCE(archived, 0)=0")
    if "archived_at" in columns:
        clauses.append("archived_at IS NULL")
    return " AND ".join(clauses) if clauses else "1=1"


def resolve_rollout_path(raw_path: str, codex_home: Path) -> Path:
    rollout = Path(raw_path).expanduser()
    if not rollout.is_absolute():
        return codex_home / rollout
    default_home = default_codex_home().resolve()
    target_home = codex_home.resolve()
    if target_home != default_home:
        try:
            return target_home / rollout.resolve().relative_to(default_home)
        except ValueError:
            pass
    return rollout


def affected_predicate() -> str:
    return "(length(title) > ? OR (title = first_user_message AND length(first_user_message) > ?))"


def inventory(codex_home: Path) -> list[ThreadRow]:
    db_path = codex_home / "state_5.sqlite"
    con = sqlite_connect(db_path)
    try:
        columns = table_columns(con, "threads")
        where = active_where(columns)
        required = {"id", "rollout_path", "title", "first_user_message", "source", "cwd"}
        missing = required - columns
        if missing:
            raise RuntimeError(f"threads table missing required columns: {', '.join(sorted(missing))}")
        updated_expr = "updated_at_ms" if "updated_at_ms" in columns else ("updated_at" if "updated_at" in columns else "NULL")
        query = f"""
            SELECT id, rollout_path, title, first_user_message, source, cwd, {updated_expr}
            FROM threads
            WHERE {where} AND {affected_predicate()}
            ORDER BY {updated_expr} DESC, id DESC
        """
        rows = []
        for row in con.execute(query, (TITLE_MAX_LEN, TITLE_MAX_LEN)):
            rollout = resolve_rollout_path(row[1], codex_home)
            rows.append(
                ThreadRow(
                    id=row[0],
                    rollout_path=rollout,
                    title=row[2] or "",
                    first_user_message=row[3] or "",
                    source=row[4] or "",
                    cwd=row[5] or "",
                    updated_at_ms=row[6],
                )
            )
        return rows
    finally:
        con.close()


def latest_session_index_titles(path: Path) -> dict[str, str]:
    latest: dict[str, str] = {}
    if not path.exists():
        return latest
    for obj in load_jsonl(path):
        thread_id = obj.get("id") or obj.get("thread_id")
        title = obj.get("thread_name") or obj.get("title")
        if isinstance(thread_id, str) and isinstance(title, str):
            latest[thread_id] = title
    return latest


def latest_rollout_title(path: Path) -> str | None:
    latest = None
    for obj in load_jsonl(path):
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "ThreadNameUpdated":
            value = obj.get("thread_name") or obj.get("title")
        elif payload.get("type") == THREAD_EVENT_TYPE:
            value = payload.get("thread_name") or payload.get("title") or payload.get("name")
        else:
            value = None
        if isinstance(value, str):
            latest = value
    return latest


def title_event(thread_id: str, title: str, when: str | None = None) -> dict[str, Any]:
    when = when or iso_z()
    return {
        "timestamp": when,
        "type": "event_msg",
        "payload": {
            "type": THREAD_EVENT_TYPE,
            "thread_id": thread_id,
            "thread_name": title,
            "title": title,
            "event_id": str(uuid.uuid4()),
            "updated_at": when,
        },
    }


def session_index_event(thread_id: str, title: str, when: str | None = None) -> dict[str, Any]:
    return {"id": thread_id, "thread_name": title, "updated_at": when or iso_z()}


def validate_inputs(codex_home: Path, rows: list[ThreadRow]) -> None:
    db_path = codex_home / "state_5.sqlite"
    result = integrity_check(db_path)
    if result.lower() != "ok":
        raise RuntimeError(f"SQLite integrity check failed before mutation: {result}")
    session_index = codex_home / "session_index.jsonl"
    if session_index.exists():
        load_jsonl(session_index)
    for rollout in sorted({row.rollout_path for row in rows}):
        load_jsonl(rollout)


def collect_backup_files(codex_home: Path, rows: list[ThreadRow]) -> list[Path]:
    candidates = [
        codex_home / "state_5.sqlite",
        codex_home / "state_5.sqlite-wal",
        codex_home / "state_5.sqlite-shm",
        codex_home / "session_index.jsonl",
    ]
    candidates.extend(sorted({row.rollout_path for row in rows}))
    seen: set[Path] = set()
    files = []
    for path in candidates:
        resolved = path.resolve()
        if path.exists() and resolved not in seen:
            seen.add(resolved)
            files.append(path)
    return files


def metrics_title(codex_home: Path, iterations: int = 40) -> dict[str, Any]:
    db_path = codex_home / "state_5.sqlite"
    con = sqlite_connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        columns = table_columns(con, "threads")
        where = active_where(columns)
        summary = con.execute(
            f"""
            SELECT
              COUNT(*) AS active_row_count,
              COALESCE(SUM(length(title)), 0) AS total_active_title_chars,
              COALESCE(MAX(length(title)), 0) AS max_active_title_length,
              SUM(CASE WHEN length(title)>? THEN 1 ELSE 0 END) AS active_titles_over_120,
              SUM(CASE WHEN title=first_user_message THEN 1 ELSE 0 END) AS active_rows_title_equals_first_user_message
            FROM threads
            WHERE {where}
            """,
            (TITLE_MAX_LEN,),
        ).fetchone()
        title_query = f"""
            SELECT id, title, source, cwd, updated_at_ms
            FROM threads
            WHERE {where}
            ORDER BY updated_at_ms DESC, id DESC
            LIMIT 200
        """
        full_query = f"""
            SELECT id, title, first_user_message, source, cwd, updated_at_ms
            FROM threads
            WHERE {where}
            ORDER BY updated_at_ms DESC, id DESC
            LIMIT 200
        """
        title_rows, title_timings, title_json_timings, title_bytes = timed_query(con, title_query, iterations)
        _, full_timings, full_json_timings, full_bytes = timed_query(con, full_query, iterations)
        return {
            "phase": "title",
            "codex_home": str(codex_home),
            "timestamp": iso_z(),
            "sqlite_integrity": integrity_check(db_path),
            **dict(summary),
            "title_list_query_payload_bytes": title_bytes,
            "title_list_query_median_ms": median_ms(title_timings),
            "title_list_query_p95_ms": p95_ms(title_timings),
            "title_list_json_encode_median_ms": median_ms(title_json_timings),
            "title_list_json_encode_p95_ms": p95_ms(title_json_timings),
            "full_list_item_payload_bytes": full_bytes,
            "full_list_item_query_median_ms": median_ms(full_timings),
            "full_list_item_query_p95_ms": p95_ms(full_timings),
            "full_list_item_json_encode_median_ms": median_ms(full_json_timings),
            "full_list_item_json_encode_p95_ms": p95_ms(full_json_timings),
            "sample_thread_ids": [row["id"] for row in title_rows[:10]],
        }
    finally:
        con.close()


def timed_query(con: sqlite3.Connection, query: str, iterations: int) -> tuple[list[dict[str, Any]], list[int], list[int], int]:
    query_timings: list[int] = []
    json_timings: list[int] = []
    rows: list[dict[str, Any]] = []
    encoded = b"[]"
    for _ in range(max(iterations, 1)):
        start = time.perf_counter_ns()
        fetched = [dict(row) for row in con.execute(query).fetchall()]
        query_timings.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        encoded = json.dumps(fetched, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        json_timings.append(time.perf_counter_ns() - start)
        rows = fetched
    return rows, query_timings, json_timings, len(encoded)


def median_ms(values: list[int]) -> float:
    return round(statistics.median(values) / 1_000_000, 3) if values else 0.0


def p95_ms(values: list[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95)))
    return round(ordered[index] / 1_000_000, 3)


def thread_loading_metrics(codex_home: Path) -> dict[str, Any]:
    base: dict[str, Any] = {
        "phase": "thread-loading",
        "codex_home": str(codex_home),
        "timestamp": iso_z(),
        "selected_large_threads": [],
        "app_server_results": [],
        "cdp_first_visible_content_ms": None,
        "cdp_settled_thread_shell_ms": None,
        "long_task_count": None,
        "total_long_task_duration_ms": None,
        "max_long_task_duration_ms": None,
        "js_heap_before_bytes": None,
        "js_heap_after_bytes": None,
        "js_heap_post_gc_bytes": None,
        "older_turns_loaded_automatically": None,
        "note": "",
    }
    try:
        base.update(measure_app_server_threads(codex_home))
    except Exception as exc:
        base["app_server_note"] = f"app-server measurement unavailable: {exc}"
    base["note"] = (
        "App-server read/list timings are measured through `codex app-server --listen stdio://` "
        "when available. CDP renderer timings require a reachable Codex Desktop renderer target; "
        "the tool does not fabricate first-visible, long-task, or heap results when CDP is not available."
    )
    return base


def select_large_threads(codex_home: Path, limit: int = 3) -> list[dict[str, Any]]:
    con = sqlite_connect(codex_home / "state_5.sqlite")
    try:
        columns = table_columns(con, "threads")
        where = active_where(columns)
        rows = []
        for row in con.execute(
            f"""
            SELECT id, length(first_user_message), length(title), rollout_path
            FROM threads
            WHERE {where}
            ORDER BY length(first_user_message) DESC, updated_at_ms DESC
            LIMIT ?
            """,
            (limit,),
        ):
            rows.append(
                {
                    "thread_id": row[0],
                    "first_user_message_chars": row[1] or 0,
                    "title_chars": row[2] or 0,
                    "rollout_path": str(resolve_rollout_path(row[3], codex_home)),
                }
            )
        return rows
    finally:
        con.close()


def prepare_app_server_home(codex_home: Path, temp_root: Path) -> Path:
    home_parent = temp_root / "home"
    server_home = home_parent / ".codex"
    server_home.mkdir(parents=True)
    for child in codex_home.iterdir():
        if child.name.startswith("state_5.sqlite"):
            continue
        target = server_home / child.name
        try:
            target.symlink_to(child, target_is_directory=child.is_dir())
        except OSError:
            if child.is_dir():
                shutil.copytree(child, target, symlinks=True)
            else:
                shutil.copy2(child, target)
    shutil.copy2(codex_home / "state_5.sqlite", server_home / "state_5.sqlite")
    for suffix in ("-wal", "-shm"):
        sidecar = codex_home / f"state_5.sqlite{suffix}"
        if sidecar.exists():
            shutil.copy2(sidecar, server_home / sidecar.name)
    rewrite_rollout_paths_for_shadow_home(server_home)
    return home_parent


def rewrite_rollout_paths_for_shadow_home(server_home: Path) -> None:
    db_path = server_home / "state_5.sqlite"
    con = sqlite_connect(db_path)
    try:
        default_home = str(default_codex_home().resolve())
        replacement_home = str(server_home.resolve())
        con.execute("BEGIN")
        con.execute(
            """
            UPDATE threads
            SET rollout_path = ? || substr(rollout_path, ? + 1)
            WHERE rollout_path LIKE ? || '/%'
            """,
            (replacement_home, len(default_home), default_home),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def measure_app_server_threads(codex_home: Path) -> dict[str, Any]:
    selected = select_large_threads(codex_home)
    if not selected:
        return {"selected_large_threads": [], "app_server_results": [], "app_server_note": "no active threads found"}
    with tempfile.TemporaryDirectory(prefix="codex-perf-appserver-") as tmp:
        home_parent = prepare_app_server_home(codex_home, Path(tmp))
        client = AppServerClient(home_parent)
        try:
            init = client.request(
                "initialize",
                {
                    "clientInfo": {"name": "codex-perf", "title": "Codex Perf", "version": TOOL_VERSION},
                    "capabilities": {"experimentalApi": True},
                },
            )
            results = []
            for item in selected:
                thread_id = item["thread_id"]
                false_result = client.timed_request("thread/read", {"threadId": thread_id, "includeTurns": False})
                true_result = client.timed_request("thread/read", {"threadId": thread_id, "includeTurns": True})
                turns_result = client.timed_request(
                    "thread/turns/list",
                    {"threadId": thread_id, "limit": 20, "sortDirection": "desc"},
                )
                results.append(
                    {
                        **item,
                        "thread_read_include_turns_false_ms": false_result["duration_ms"],
                        "thread_read_include_turns_false_bytes": false_result["payload_bytes"],
                        "thread_read_include_turns_true_ms": true_result["duration_ms"],
                        "thread_read_include_turns_true_bytes": true_result["payload_bytes"],
                        "thread_turns_list_ms": turns_result["duration_ms"],
                        "thread_turns_list_bytes": turns_result["payload_bytes"],
                    }
                )
            first = results[0]
            return {
                "app_server_initialize": init.get("result", {}),
                "selected_large_threads": selected,
                "app_server_results": results,
                "app_server_read_include_turns_false_ms": first["thread_read_include_turns_false_ms"],
                "app_server_read_include_turns_false_bytes": first["thread_read_include_turns_false_bytes"],
                "app_server_read_include_turns_true_ms": first["thread_read_include_turns_true_ms"],
                "app_server_read_include_turns_true_bytes": first["thread_read_include_turns_true_bytes"],
                "thread_turns_list_ms": first["thread_turns_list_ms"],
                "thread_turns_list_bytes": first["thread_turns_list_bytes"],
                "app_server_note": "measured through isolated HOME wrapper with a copied state database and remapped rollout paths",
            }
        finally:
            client.close()


class AppServerClient:
    def __init__(self, home_parent: Path):
        env = os.environ.copy()
        env["HOME"] = str(home_parent)
        self.proc = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            bufsize=1,
        )
        self.next_id = 1

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def timed_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        start = time.perf_counter_ns()
        response = self.request(method, params, timeout=60)
        duration_ms = round((time.perf_counter_ns() - start) / 1_000_000, 3)
        return {"duration_ms": duration_ms, "payload_bytes": len(json.dumps(response, separators=(",", ":")).encode("utf-8"))}

    def request(self, method: str, params: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
        if self.proc.stdin is None or self.proc.stdout is None or self.proc.stderr is None:
            raise RuntimeError("app-server stdio unavailable")
        request_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        stderr_lines: list[str] = []
        while time.time() < deadline:
            ready, _, _ = select.select([self.proc.stdout, self.proc.stderr], [], [], 0.25)
            for stream in ready:
                line = stream.readline()
                if not line:
                    continue
                if stream is self.proc.stderr:
                    stderr_lines.append(line.strip())
                    continue
                response = json.loads(line)
                if response.get("id") == request_id:
                    if "error" in response:
                        raise RuntimeError(f"{method} failed: {response['error']}")
                    return response
            if self.proc.poll() is not None:
                raise RuntimeError(f"app-server exited with {self.proc.returncode}: {'; '.join(stderr_lines[-5:])}")
        raise TimeoutError(f"timed out waiting for {method}: {'; '.join(stderr_lines[-5:])}")


def write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def write_metrics_summary(path: Path, before: dict[str, Any] | None, after: dict[str, Any] | None, thread: dict[str, Any] | None, extra: str = "") -> None:
    lines = ["# Codex Perf Verification Summary", "", f"Generated: {iso_z()}", ""]
    if before:
        lines.extend(metric_lines("Before", before))
    if after:
        lines.extend(metric_lines("After Title Repair", after))
    if thread:
        lines.extend(["## Thread Loading Workaround", ""])
        for key in (
            "app_server_read_include_turns_false_ms",
            "app_server_read_include_turns_false_bytes",
            "app_server_read_include_turns_true_ms",
            "app_server_read_include_turns_true_bytes",
            "thread_turns_list_ms",
            "thread_turns_list_bytes",
            "app_server_note",
        ):
            lines.append(f"- `{key}`: {thread.get(key)!r}")
        for key in (
            "cdp_first_visible_content_ms",
            "cdp_settled_thread_shell_ms",
            "long_task_count",
            "total_long_task_duration_ms",
            "max_long_task_duration_ms",
            "js_heap_before_bytes",
            "js_heap_after_bytes",
            "js_heap_post_gc_bytes",
            "older_turns_loaded_automatically",
            "note",
        ):
            lines.append(f"- `{key}`: {thread.get(key)!r}")
        lines.append("")
    if extra:
        lines.extend(["## Autonomous Live-Repair Strategy", "", extra.strip(), ""])
    atomic_write_text(path, "\n".join(lines))


def metric_lines(title: str, metrics: dict[str, Any]) -> list[str]:
    keys = [
        "active_row_count",
        "total_active_title_chars",
        "max_active_title_length",
        "active_titles_over_120",
        "active_rows_title_equals_first_user_message",
        "title_list_query_payload_bytes",
        "title_list_query_median_ms",
        "title_list_query_p95_ms",
        "title_list_json_encode_median_ms",
        "title_list_json_encode_p95_ms",
    ]
    lines = [f"## {title}", ""]
    lines.extend(f"- `{key}`: {metrics.get(key)!r}" for key in keys)
    lines.append("")
    return lines


def create_backup(
    codex_home: Path,
    backup_dir: Path | None,
    command: str,
    rows: list[ThreadRow] | None = None,
    killed: list[ProcessInfo] | None = None,
    metrics_before: dict[str, Any] | None = None,
) -> Path:
    rows = rows if rows is not None else inventory(codex_home)
    validate_inputs(codex_home, rows)
    root = (backup_dir.expanduser() if backup_dir else codex_home / "backups").resolve()
    backup_root = root / f"thread-perf-fix-{timestamp_slug()}"
    backup_root.mkdir(parents=True, exist_ok=False)

    file_records = []
    for original in collect_backup_files(codex_home, rows):
        dest = backup_destination(backup_root, original, codex_home)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, dest)
        file_records.append(file_record(original, dest))

    if metrics_before is None:
        metrics_before = metrics_title(codex_home)
    write_json(backup_root / "metrics-before.json", metrics_before)

    manifest = {
        "tool_version": TOOL_VERSION,
        "command": command,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version,
        },
        "codex_home": str(codex_home),
        "timestamp": iso_z(),
        "sqlite_integrity_before": integrity_check(codex_home / "state_5.sqlite"),
        "killed_codex_processes": [process_to_dict(p) for p in (killed or [])],
        "affected_thread_ids": [row.id for row in rows],
        "affected_file_paths": sorted(str(path) for path in {row.rollout_path for row in rows}),
        "threads": [
            {
                "id": row.id,
                "rollout_path": str(row.rollout_path),
                "old_title_length": len(row.title),
                "new_title_length": len(summarize_for_label(row.first_user_message)),
                "title_equals_first_user_message": row.title == row.first_user_message,
            }
            for row in rows
        ],
        "files": file_records,
        "restore_instructions": f"{sys.executable} scripts/fix-codex-perf.py restore --backup {backup_root}",
    }
    write_json(backup_root / MANIFEST_NAME, manifest)
    return backup_root


def process_to_dict(process: ProcessInfo) -> dict[str, Any]:
    return {"pid": process.pid, "name": process.name, "command": process.command}


def parse_process_listing(system: str, output: str) -> list[ProcessInfo]:
    if system.lower().startswith("win"):
        return parse_windows_processes(output)
    return parse_unix_processes(output)


def parse_unix_processes(output: str) -> list[ProcessInfo]:
    matches: list[ProcessInfo] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if not parts or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        command = parts[1] if len(parts) > 1 else ""
        name = Path(command.split()[0]).name if command.split() else ""
        process = ProcessInfo(pid=pid, name=name, command=command)
        if is_codex_process(process):
            matches.append(process)
    return matches


def parse_windows_processes(output: str) -> list[ProcessInfo]:
    matches: list[ProcessInfo] = []
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return matches
    try:
        reader = csv.DictReader(lines)
        for row in reader:
            pid_text = row.get("ProcessId") or row.get("PID") or ""
            if not pid_text.strip().isdigit():
                continue
            process = ProcessInfo(
                pid=int(pid_text),
                name=(row.get("Name") or row.get("ImageName") or row.get("Image Name") or "").strip('"'),
                command=(row.get("CommandLine") or row.get("Name") or row.get("ImageName") or row.get("Image Name") or "").strip('"'),
            )
            if is_codex_process(process):
                matches.append(process)
    except csv.Error:
        for line in lines:
            if "codex" not in line.lower():
                continue
            fields = line.split()
            pid = next((int(f) for f in fields if f.isdigit()), None)
            if pid is None:
                continue
            process = ProcessInfo(pid=pid, name=fields[0], command=line)
            if is_codex_process(process):
                matches.append(process)
    return matches


def is_codex_process(process: ProcessInfo) -> bool:
    command = process.command
    lower = command.lower()
    name = process.name.lower()
    if not lower:
        return False
    own_markers = ("fix-codex-perf.py", "pgrep -afil codex", "find ~/.codex", "dangerous-command-blocker.py")
    if any(marker in lower for marker in own_markers):
        return False
    if "codex computer use.app" in lower or "skycomputeruseclient" in lower:
        return False
    if "/codex helper.app/" in lower or "/codex helper " in lower:
        return False
    if name in {"codex", "codex.exe", "codex.cmd"}:
        return True
    if "/codex.app/contents/macos/codex" in lower or "\\codex\\codex.exe" in lower:
        return True
    tokens = lower.replace("\\", "/").split()
    if any(token.endswith("/codex") or token.endswith("/codex.exe") or token.endswith("/codex.cmd") for token in tokens):
        return True
    return False


def detect_codex_processes() -> list[ProcessInfo]:
    system = platform.system()
    if system == "Windows":
        output = run_capture(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine | ConvertTo-Csv -NoTypeInformation",
            ]
        )
        if output is None:
            output = run_capture(["tasklist", "/v", "/fo", "csv"]) or ""
    else:
        output = run_capture(["pgrep", "-afil", "codex"])
        if output is None:
            output = run_capture(["ps", "-axo", "pid=,command="]) or ""
    current = os.getpid()
    return [p for p in parse_process_listing(system, output) if p.pid != current]


def run_capture(args: list[str]) -> str | None:
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except (FileNotFoundError, OSError):
        return None
    return result.stdout


def process_safety_needed(codex_home: Path) -> bool:
    return codex_home.resolve() == default_codex_home().resolve()


def preflight_process_safety(codex_home: Path, yes: bool) -> list[ProcessInfo]:
    if not process_safety_needed(codex_home):
        return []
    matches = detect_codex_processes()
    if not matches:
        return []
    print("Running Codex processes detected:")
    for proc in matches:
        print(f"  PID {proc.pid}\t{proc.name}\t{proc.command}")
    if not yes:
        answer = input("Kill all running Codex instances? [y/N] ")
        if answer.strip().lower() != "y":
            raise SystemExit("Refusing to mutate live Codex state while Codex is running.")
    return kill_processes(matches)


def kill_processes(processes: list[ProcessInfo]) -> list[ProcessInfo]:
    killed = []
    if platform.system() == "Windows":
        for proc in processes:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            killed.append(proc)
        time.sleep(2)
        for proc in processes:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return killed
    for proc in processes:
        try:
            os.kill(proc.pid, signal.SIGTERM)
            killed.append(proc)
        except ProcessLookupError:
            pass
    time.sleep(2)
    for proc in processes:
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            continue
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return killed


def command_backup(args: argparse.Namespace) -> int:
    codex_home = resolve_codex_home(args.codex_home)
    backup = create_backup(codex_home, Path(args.backup_dir) if args.backup_dir else None, "backup")
    print(f"Backup created: {backup}")
    print(f"Manifest: {backup / MANIFEST_NAME}")
    return 0


def command_repair(args: argparse.Namespace) -> int:
    codex_home = resolve_codex_home(args.codex_home)
    killed = preflight_process_safety(codex_home, args.yes)
    before = metrics_title(codex_home)
    rows = inventory(codex_home)
    validate_inputs(codex_home, rows)
    backup = create_backup(
        codex_home,
        Path(args.backup_dir) if args.backup_dir else None,
        "repair",
        rows,
        killed,
        before,
    )
    changes = apply_repair(codex_home, rows, backup)
    after = metrics_title(codex_home)
    write_json(backup / "metrics-after-title-repair.json", after)
    thread_metrics = thread_loading_metrics(codex_home)
    write_json(backup / "metrics-after-thread-loading-workaround.json", thread_metrics)
    live_note = live_strategy_note(codex_home)
    write_metrics_summary(backup / "metrics-summary.md", before, after, thread_metrics, live_note)
    update_manifest_after_repair(backup, changes, integrity_check(codex_home / "state_5.sqlite"))
    print(f"Backup created: {backup}")
    print(f"Repair summary: repaired={changes['sqlite_rows_updated']} jsonl_events_appended={changes['jsonl_events_appended']} session_index_events_appended={changes['session_index_events_appended']}")
    print(f"SQLite integrity after: {changes['sqlite_integrity_after']}")
    print(f"Metrics summary: {backup / 'metrics-summary.md'}")
    return 0


def apply_repair(codex_home: Path, rows: list[ThreadRow], backup: Path) -> dict[str, Any]:
    if not rows:
        return {
            "sqlite_rows_updated": 0,
            "jsonl_events_appended": 0,
            "session_index_events_appended": 0,
            "thread_changes": [],
            "sqlite_integrity_after": integrity_check(codex_home / "state_5.sqlite"),
        }
    index_path = codex_home / "session_index.jsonl"
    latest_index = latest_session_index_titles(index_path)
    now = iso_z()
    thread_changes: list[dict[str, Any]] = []
    jsonl_events = 0
    index_events = 0

    for row in rows:
        new_title = summarize_for_label(row.first_user_message)
        existing_rollout_title = latest_rollout_title(row.rollout_path)
        event_id = None
        if existing_rollout_title != new_title:
            event = title_event(row.id, new_title, now)
            event_id = event["payload"]["event_id"]
            append_jsonl(row.rollout_path, event)
            jsonl_events += 1
        if latest_index.get(row.id) != new_title:
            append_jsonl(index_path, session_index_event(row.id, new_title, now))
            index_events += 1
            latest_index[row.id] = new_title
        thread_changes.append(
            {
                "id": row.id,
                "rollout_path": str(row.rollout_path),
                "old_title_length": len(row.title),
                "new_title_length": len(new_title),
                "title_equals_first_user_message": row.title == row.first_user_message,
                "appended_jsonl_event_id": event_id,
                "appended_jsonl_event_timestamp": now if event_id else None,
            }
        )

    db_path = codex_home / "state_5.sqlite"
    con = sqlite_connect(db_path)
    updated = 0
    try:
        con.execute("BEGIN")
        for row in rows:
            new_title = summarize_for_label(row.first_user_message)
            if row.title != new_title:
                cur = con.execute("UPDATE threads SET title=? WHERE id=?", (new_title, row.id))
                updated += cur.rowcount
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    after = integrity_check(db_path)
    if after.lower() != "ok":
        raise RuntimeError(f"SQLite integrity check failed after mutation: {after}")
    return {
        "sqlite_rows_updated": updated,
        "jsonl_events_appended": jsonl_events,
        "session_index_events_appended": index_events,
        "thread_changes": thread_changes,
        "sqlite_integrity_after": after,
    }


def update_manifest_after_repair(backup: Path, changes: dict[str, Any], sqlite_integrity_after: str) -> None:
    manifest_path = backup / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["repair"] = changes
    manifest["sqlite_integrity_after"] = sqlite_integrity_after
    write_json(manifest_path, manifest)


def live_strategy_note(codex_home: Path) -> str:
    if process_safety_needed(codex_home):
        return (
            "This run operated on the default Codex home. Process safety required either no matching "
            "Codex processes or explicit `-y` termination before mutation. The manifest records any killed PIDs."
        )
    return (
        "This run intentionally avoided live `~/.codex` mutation by operating against a copied/custom "
        "`--codex-home`. Because the active agent is itself a Codex process and non-interactive desktop "
        "resume was not proven in this run, the safe autonomous path is copied-home validation rather than "
        "terminating the live session."
    )


def command_restore(args: argparse.Namespace) -> int:
    backup = Path(args.backup).expanduser().resolve()
    manifest_path = backup / MANIFEST_NAME
    if not manifest_path.exists():
        raise SystemExit(f"Missing manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    codex_home = resolve_codex_home(args.codex_home or manifest.get("codex_home"))
    killed = preflight_process_safety(codex_home, args.yes)
    restored = []
    for record in manifest.get("files", []):
        original = Path(record["original_path"])
        if args.codex_home:
            try:
                rel = original.resolve().relative_to(Path(manifest["codex_home"]).resolve())
                original = codex_home / rel
            except ValueError:
                pass
        source = Path(record["backup_path"])
        target = original
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=str(target.parent), delete=False) as handle:
            with source.open("rb") as src:
                shutil.copyfileobj(src, handle)
            tmp = Path(handle.name)
        os.replace(tmp, target)
        actual = sha256_file(target)
        if actual != record["sha256"]:
            raise RuntimeError(f"Restored hash mismatch for {target}: {actual} != {record['sha256']}")
        restored.append(str(target))
    sqlite_result = integrity_check(codex_home / "state_5.sqlite")
    if sqlite_result.lower() != "ok":
        raise RuntimeError(f"SQLite integrity check failed after restore: {sqlite_result}")
    print(f"Restore summary: restored={len(restored)} sqlite_integrity={sqlite_result}")
    if killed:
        print(f"Killed Codex processes: {len(killed)}")
    return 0


def command_measure(args: argparse.Namespace) -> int:
    codex_home = resolve_codex_home(args.codex_home)
    root = (Path(args.backup_dir).expanduser() if args.backup_dir else codex_home / "backups").resolve()
    output = root / f"thread-perf-measure-{timestamp_slug()}"
    output.mkdir(parents=True, exist_ok=False)
    before = metrics_title(codex_home) if args.phase in {"title", "all"} else None
    thread = thread_loading_metrics(codex_home) if args.phase in {"thread-loading", "all"} else None
    if before:
        write_json(output / "metrics-before.json", before)
        write_json(output / "metrics-after-title-repair.json", before)
    if thread:
        write_json(output / "metrics-after-thread-loading-workaround.json", thread)
    write_metrics_summary(output / "metrics-summary.md", before, before, thread, live_strategy_note(codex_home))
    print(f"Metrics written: {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", help="Codex home directory; defaults to ~/.codex")
    sub = parser.add_subparsers(dest="command", required=True)

    backup = sub.add_parser("backup", help="Create a timestamped non-mutating backup package")
    backup.add_argument("--backup-dir", help="Directory that will contain timestamped backup packages")
    backup.set_defaults(func=command_backup)

    repair = sub.add_parser("repair", help="Backup, repair title metadata, and write metrics")
    repair.add_argument("--backup-dir", help="Directory that will contain timestamped backup packages")
    repair.add_argument("-y", "--yes", action="store_true", help="Kill matching live Codex processes without prompting")
    repair.set_defaults(func=command_repair)

    restore = sub.add_parser("restore", help="Restore files from a selected backup manifest")
    restore.add_argument("--backup", required=True, help="Backup directory containing manifest.json")
    restore.add_argument("-y", "--yes", action="store_true", help="Kill matching live Codex processes without prompting")
    restore.set_defaults(func=command_restore)

    measure = sub.add_parser("measure", help="Write title and/or thread-loading measurement artifacts")
    measure.add_argument("--backup-dir", help="Directory that will contain timestamped measurement output")
    measure.add_argument("--phase", choices=["title", "thread-loading", "all"], default="all")
    measure.set_defaults(func=command_measure)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SystemExit:
        raise
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
