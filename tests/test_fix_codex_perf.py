import hashlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fix-codex-perf.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("fix_codex_perf", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_home(root: Path, absolute_rollout_path: bool = False) -> tuple[Path, str]:
    home = root / "codex-home"
    sessions = home / "sessions" / "2026" / "01" / "02"
    sessions.mkdir(parents=True)
    thread_id = "019abc00-0000-7000-8000-000000000001"
    first_message = "  " + ("x" * 121) + "\nsecond line must be ignored"
    rollout = sessions / f"rollout-2026-01-02T03-04-05-{thread_id}.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-01-02T03:04:05.000Z",
                "type": "event_msg",
                "payload": {"type": "UserMessage", "message": first_message},
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    (home / "session_index.jsonl").write_text(
        json.dumps(
            {
                "id": thread_id,
                "thread_name": first_message,
                "updated_at": "2026-01-02T03:04:05.000000Z",
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(home / "state_5.sqlite")
    stored_rollout = rollout
    if absolute_rollout_path:
        stored_rollout = Path.home() / ".codex" / rollout.relative_to(home)
    con.execute(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            source TEXT NOT NULL,
            model_provider TEXT NOT NULL,
            cwd TEXT NOT NULL,
            title TEXT NOT NULL,
            sandbox_policy TEXT NOT NULL,
            approval_mode TEXT NOT NULL,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            has_user_event INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            archived_at INTEGER,
            first_user_message TEXT NOT NULL DEFAULT '',
            created_at_ms INTEGER,
            updated_at_ms INTEGER
        )
        """
    )
    con.execute(
        """
        INSERT INTO threads
        (id, rollout_path, created_at, updated_at, source, model_provider, cwd,
         title, sandbox_policy, approval_mode, first_user_message, updated_at_ms)
        VALUES (?, ?, 1, 2, 'cli', 'openai', ?, ?, 'danger-full-access', 'never', ?, 2)
        """,
        (thread_id, str(stored_rollout), str(ROOT), first_message, first_message),
    )
    con.commit()
    con.close()
    return home, thread_id


class TitleSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_rust_parity_title_summary(self):
        summarize = self.tool.summarize_for_label
        self.assertEqual(summarize(""), "")
        self.assertEqual(summarize("  hello  "), "hello")
        self.assertEqual(summarize("first\nsecond"), "first")
        self.assertEqual(summarize("first\r\nsecond"), "first")
        self.assertEqual(summarize("a\rb\nsecond"), "a\rb")
        self.assertEqual(summarize("x" * 120), "x" * 120)
        self.assertEqual(summarize("x" * 121), ("x" * 117) + "...")
        self.assertEqual(summarize("🙂" * 121), ("🙂" * 117) + "...")


class ProcessDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()

    def test_macos_and_linux_process_matching_ignores_paths_and_hooks(self):
        output = "\n".join(
            [
                "123 /Applications/Codex.app/Contents/MacOS/Codex",
                "124 /Users/me/.bun/bin/codex app-server --analytics-default-enabled",
                "125 node /Users/me/.bun/bin/codex --dangerously-bypass-approvals-and-sandbox",
                "126 bash /Users/me/project/codex-helper/run.sh",
                "127 python3 scripts/fix-codex-perf.py repair",
                "128 /Applications/Codex Computer Use.app/Contents/MacOS/Helper mcp",
                "129 /Applications/Codex.app/Contents/Frameworks/Codex Helper.app/Contents/MacOS/Codex Helper --type=renderer",
            ]
        )
        matches = self.tool.parse_process_listing("Darwin", output)
        self.assertEqual([p.pid for p in matches], [123, 124, 125])

    def test_windows_process_matching(self):
        output = "\n".join(
            [
                "ProcessId,Name,CommandLine",
                '44,"Codex.exe","C:\\\\Users\\\\me\\\\AppData\\\\Local\\\\Programs\\\\Codex\\\\Codex.exe"',
                '45,"codex.exe","C:\\\\Users\\\\me\\\\AppData\\\\Roaming\\\\npm\\\\codex.cmd app-server"',
                '46,"powershell.exe","Get-Content C:\\\\tmp\\\\codex-notes.txt"',
            ]
        )
        matches = self.tool.parse_process_listing("Windows", output)
        self.assertEqual([p.pid for p in matches], [44, 45])

    def test_windows_tasklist_process_matching(self):
        output = "\n".join(
            [
                '"Image Name","PID","Session Name","Session#","Mem Usage"',
                '"codex.exe","144","Console","1","10,000 K"',
                '"powershell.exe","145","Console","1","10,000 K"',
            ]
        )
        matches = self.tool.parse_process_listing("Windows", output)
        self.assertEqual([p.pid for p in matches], [144])


class BackupRepairRestoreTests(unittest.TestCase):
    def test_backup_repair_idempotence_and_restore_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home, thread_id = make_home(tmp_path)
            backup_dir = tmp_path / "backups"
            db = home / "state_5.sqlite"
            index = home / "session_index.jsonl"
            rollout = next((home / "sessions").rglob("*.jsonl"))
            original_hashes = {p: sha256(p) for p in (db, index, rollout)}

            backup = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--codex-home",
                    str(home),
                    "backup",
                    "--backup-dir",
                    str(backup_dir),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertIn("Backup created:", backup.stdout)
            self.assertEqual(original_hashes, {p: sha256(p) for p in original_hashes})

            repair = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--codex-home",
                    str(home),
                    "repair",
                    "--backup-dir",
                    str(backup_dir),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertIn("repaired=1", repair.stdout)
            repair_backup = Path(
                next(line.split(":", 1)[1].strip() for line in repair.stdout.splitlines() if line.startswith("Backup created:"))
            )

            con = sqlite3.connect(db)
            title, first_user_message = con.execute(
                "SELECT title, first_user_message FROM threads WHERE id=?", (thread_id,)
            ).fetchone()
            con.close()
            self.assertEqual(len(title), 120)
            self.assertTrue(title.endswith("..."))
            self.assertNotEqual(title, first_user_message)

            self.assertEqual(original_hashes[rollout], sha256(rollout))
            index_lines = [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines()]
            title_events = [line for line in index_lines if line.get("id") == thread_id]
            self.assertEqual(len(title_events), 2)
            self.assertEqual(title_events[-1]["thread_name"], title)
            self.assertIn("updated_at", title_events[-1])

            second = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--codex-home",
                    str(home),
                    "repair",
                    "--backup-dir",
                    str(backup_dir),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertIn("repaired=0", second.stdout)
            index_events_after_second = [
                json.loads(line)
                for line in index.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("id") == thread_id
            ]
            self.assertEqual(len(index_events_after_second), 2)

            con = sqlite3.connect(db)
            con.execute("UPDATE threads SET title=first_user_message WHERE id=?", (thread_id,))
            con.commit()
            con.close()
            third = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--codex-home",
                    str(home),
                    "repair",
                    "--backup-dir",
                    str(backup_dir),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertIn("repaired=1", third.stdout)
            index_events_after_rewrite = [
                json.loads(line)
                for line in index.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("id") == thread_id
            ]
            self.assertEqual(len(index_events_after_rewrite), 2)

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--codex-home",
                    str(home),
                    "restore",
                    "--backup",
                    str(repair_backup),
                    "-y",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertEqual(original_hashes, {p: sha256(p) for p in original_hashes})

    def test_custom_home_remaps_default_home_absolute_rollout_paths(self):
        tool = load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home, _ = make_home(tmp_path, absolute_rollout_path=True)
            row = tool.inventory(home)[0]
            self.assertTrue(str(row.rollout_path.resolve()).startswith(str(home.resolve())))
            self.assertTrue(row.rollout_path.exists())

    def test_status_reports_needed_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home, _ = make_home(tmp_path)
            status = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--codex-home",
                    str(home),
                    "status",
                    "--exit-code",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(status.returncode, 2)
            self.assertIn("Repair needed: yes", status.stdout)
            self.assertIn("Affected active threads: 1", status.stdout)

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--codex-home",
                    str(home),
                    "repair",
                    "--backup-dir",
                    str(tmp_path / "backups"),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            clean = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--codex-home",
                    str(home),
                    "status",
                    "--exit-code",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(clean.returncode, 0)
            self.assertIn("Repair needed: no", clean.stdout)

    def test_repair_yes_records_killed_matching_processes_in_manifest(self):
        tool = load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home, _ = make_home(tmp_path)
            backup_dir = tmp_path / "backups"
            killed = [tool.ProcessInfo(pid=4321, name="codex", command="/tmp/bin/codex app-server")]
            original_default_home = tool.default_codex_home
            original_detect = tool.detect_codex_processes
            original_kill = tool.kill_processes
            original_thread_loading_metrics = tool.thread_loading_metrics
            try:
                tool.default_codex_home = lambda: home
                tool.detect_codex_processes = lambda: killed
                tool.kill_processes = lambda processes: processes
                tool.thread_loading_metrics = lambda codex_home: {"phase": "thread-loading", "app_server_note": "fixture"}
                args = type(
                    "Args",
                    (),
                    {
                        "codex_home": str(home),
                        "backup_dir": str(backup_dir),
                        "yes": True,
                    },
                )()

                self.assertEqual(tool.command_repair(args), 0)
            finally:
                tool.default_codex_home = original_default_home
                tool.detect_codex_processes = original_detect
                tool.kill_processes = original_kill
                tool.thread_loading_metrics = original_thread_loading_metrics

            repair_backup = next(backup_dir.iterdir())
            manifest = json.loads((repair_backup / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["killed_codex_processes"],
                [{"pid": 4321, "name": "codex", "command": "/tmp/bin/codex app-server"}],
            )

    def test_repair_skips_process_detection_when_no_repair_needed_for_default_home(self):
        tool = load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            home, thread_id = make_home(tmp_path)
            clean_title = tool.summarize_for_label("  " + ("x" * 121) + "\nsecond line must be ignored")
            con = sqlite3.connect(home / "state_5.sqlite")
            con.execute("UPDATE threads SET title=? WHERE id=?", (clean_title, thread_id))
            con.commit()
            con.close()

            original_default_home = tool.default_codex_home
            original_detect = tool.detect_codex_processes
            original_thread_loading_metrics = tool.thread_loading_metrics
            try:
                tool.default_codex_home = lambda: home
                tool.detect_codex_processes = lambda: (_ for _ in ()).throw(
                    AssertionError("process detection should not run when repair is unnecessary")
                )
                tool.thread_loading_metrics = lambda codex_home: {"phase": "thread-loading", "app_server_note": "fixture"}
                args = type(
                    "Args",
                    (),
                    {
                        "codex_home": str(home),
                        "backup_dir": str(tmp_path / "backups"),
                        "yes": True,
                    },
                )()

                self.assertEqual(tool.command_repair(args), 0)
            finally:
                tool.default_codex_home = original_default_home
                tool.detect_codex_processes = original_detect
                tool.thread_loading_metrics = original_thread_loading_metrics

    def test_stop_yes_uses_process_safety(self):
        tool = load_tool()
        with tempfile.TemporaryDirectory() as tmp:
            home, _ = make_home(Path(tmp))
            killed = [tool.ProcessInfo(pid=4321, name="codex", command="/tmp/bin/codex app-server")]
            original_default_home = tool.default_codex_home
            original_detect = tool.detect_codex_processes
            original_kill = tool.kill_processes
            try:
                tool.default_codex_home = lambda: home
                tool.detect_codex_processes = lambda: killed
                tool.kill_processes = lambda processes: processes
                args = type("Args", (), {"codex_home": str(home), "yes": True})()
                self.assertEqual(tool.command_stop(args), 0)
            finally:
                tool.default_codex_home = original_default_home
                tool.detect_codex_processes = original_detect
                tool.kill_processes = original_kill


if __name__ == "__main__":
    unittest.main()
