import importlib.util
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path, PureWindowsPath


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex-perf-launch.py"
PATCH = ROOT / "renderer" / "fast-thread-loader.js"
SHELL_LAUNCHER = ROOT / "codex-perf.sh"
CMD_LAUNCHER = ROOT / "codex-perf.cmd"


def load_launcher():
    spec = importlib.util.spec_from_file_location("codex_perf_launch", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CdpLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launcher = load_launcher()

    def test_selects_codex_page_target(self):
        targets = [
            {"type": "worker", "url": "", "webSocketDebuggerUrl": "ws://worker"},
            {"type": "page", "url": "https://example.test", "webSocketDebuggerUrl": "ws://wrong"},
            {"type": "page", "url": "app://-/index.html?hostId=local", "webSocketDebuggerUrl": "ws://codex"},
        ]
        self.assertEqual(self.launcher.select_codex_page_target(targets)["webSocketDebuggerUrl"], "ws://codex")

    def test_default_cdp_port_avoids_chrome_default(self):
        self.assertEqual(self.launcher.DEFAULT_CDP_PORT, 17373)
        self.assertNotEqual(self.launcher.DEFAULT_CDP_PORT, 9222)

    def test_websocket_frame_masking_roundtrip(self):
        payload = b'{"id":1,"method":"Runtime.enable"}'
        frame = self.launcher.encode_ws_frame(payload, mask=b"\x01\x02\x03\x04")
        self.assertEqual(frame[0], 0x81)
        self.assertTrue(frame[1] & 0x80)
        self.assertEqual(self.launcher.decode_ws_frame_for_test(frame), payload)

    def test_injection_source_wraps_renderer_patch_without_state_mutation(self):
        source = self.launcher.build_injection_source(PATCH)
        self.assertIn("window.__codexPerfFastLoaderStop", source)
        self.assertIn("module.exports", source)
        self.assertIn("start(api)", source)
        self.assertIn('source: "cdp-wrapper"', source)
        self.assertIn("navigation-start", source)
        for forbidden in ["state_5.sqlite", "session_index.jsonl", "ThreadNameUpdated", "titleRepairPlan"]:
            self.assertNotIn(forbidden, source)
        for forbidden in ["threadDataUrl", "ThreadDataHttpServer"]:
            self.assertNotIn(forbidden, source)

    def test_launcher_has_no_thread_data_http_server(self):
        code = SCRIPT.read_text(encoding="utf-8")
        for forbidden in [
            "ThreadDataHttpServer",
            "http.server",
            "thread-data server",
            "--thread-data-port",
            "--no-thread-data-server",
            "thread_data_server",
        ]:
            self.assertNotIn(forbidden, code)

    def test_macos_launch_does_not_force_new_app_instance(self):
        code = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"open",', code)
        self.assertIn('"-a",', code)
        self.assertNotIn('"-na",', code)

    def test_macos_launch_suppresses_open_output(self):
        app_path = Path("/Applications/Codex.app")
        with mock.patch.object(self.launcher.subprocess, "run") as run:
            self.launcher.launch_macos_codex(app_path, 17373)

        run.assert_called_once_with(
            [
                "open",
                "-a",
                str(app_path),
                "--args",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=17373",
            ],
            check=True,
            stdout=self.launcher.subprocess.DEVNULL,
            stderr=self.launcher.subprocess.DEVNULL,
        )

    def test_windows_default_app_path_uses_local_app_data_candidates(self):
        app_path = self.launcher.default_app_path(
            system="Windows",
            environ={"LOCALAPPDATA": "C:/Users/Claire/AppData/Local"},
        )
        self.assertEqual(
            PureWindowsPath(app_path),
            PureWindowsPath("C:/Users/Claire/AppData/Local/Programs/Codex/Codex.exe"),
        )

    def test_windows_default_app_path_finds_windowsapps_msix_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Program Files"
            app_dir = root / "WindowsApps" / "OpenAI.Codex_26.429.8261.0_x64__2p2nqsd0c76g0" / "app"
            app_dir.mkdir(parents=True)
            exe = app_dir / "Codex.exe"
            exe.write_bytes(b"")

            app_path = self.launcher.default_app_path(
                system="Windows",
                environ={"ProgramFiles": str(root)},
            )

        self.assertEqual(app_path, str(exe))

    def test_windows_default_app_path_uses_appx_package_install_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            program_files = Path(tmp) / "Program Files"
            package_root = Path(tmp) / "Appx" / "OpenAI.Codex_26.429.8261.0_x64__2p2nqsd0c76g0"
            app_dir = package_root / "app"
            app_dir.mkdir(parents=True)
            exe = app_dir / "Codex.exe"
            exe.write_bytes(b"")
            completed = self.launcher.subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"{package_root}\n",
            )
            with mock.patch.object(self.launcher.subprocess, "run", return_value=completed):
                app_path = self.launcher.default_app_path(
                    system="Windows",
                    environ={
                        "ProgramFiles": str(program_files),
                        "SystemRoot": "C:/Windows",
                    },
                )

        self.assertEqual(app_path, str(exe))

    def test_windows_launch_starts_codex_exe_with_cdp_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "Codex.exe"
            app_path.write_bytes(b"")
            with mock.patch.object(self.launcher.platform, "system", return_value="Windows"):
                with mock.patch.object(self.launcher.subprocess, "Popen") as popen:
                    with mock.patch.object(self.launcher.subprocess, "run") as run:
                        self.launcher.launch_codex(app_path, 17373, None)

        popen.assert_called_once_with(
            [
                str(app_path),
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=17373",
            ],
            cwd=str(app_path.parent),
            close_fds=True,
            stdout=self.launcher.subprocess.DEVNULL,
            stderr=self.launcher.subprocess.DEVNULL,
        )
        run.assert_not_called()

    def test_windows_launch_accepts_msix_app_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "WindowsApps" / "OpenAI.Codex_26.429.8261.0_x64__2p2nqsd0c76g0" / "app"
            app_dir.mkdir(parents=True)
            exe = app_dir / "Codex.exe"
            exe.write_bytes(b"")
            with mock.patch.object(self.launcher.platform, "system", return_value="Windows"):
                with mock.patch.object(self.launcher.subprocess, "Popen") as popen:
                    self.launcher.launch_codex(app_dir, 17373, None)

        popen.assert_called_once_with(
            [
                str(exe),
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=17373",
            ],
            cwd=str(app_dir),
            close_fds=True,
            stdout=self.launcher.subprocess.DEVNULL,
            stderr=self.launcher.subprocess.DEVNULL,
        )

    def test_windows_launch_accepts_msix_package_root_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "WindowsApps" / "OpenAI.Codex_26.429.8261.0_x64__2p2nqsd0c76g0"
            app_dir = package_root / "app"
            app_dir.mkdir(parents=True)
            exe = app_dir / "Codex.exe"
            exe.write_bytes(b"")
            with mock.patch.object(self.launcher.platform, "system", return_value="Windows"):
                with mock.patch.object(self.launcher.subprocess, "Popen") as popen:
                    self.launcher.launch_codex(package_root, 17373, None)

        popen.assert_called_once_with(
            [
                str(exe),
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=17373",
            ],
            cwd=str(app_dir),
            close_fds=True,
            stdout=self.launcher.subprocess.DEVNULL,
            stderr=self.launcher.subprocess.DEVNULL,
        )

    def test_windows_launch_reports_missing_codex_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_path = Path(tmp) / "Codex.exe"
            with mock.patch.object(self.launcher.platform, "system", return_value="Windows"):
                with self.assertRaisesRegex(RuntimeError, "Codex Desktop executable not found"):
                    self.launcher.launch_codex(app_path, 17373, None)

    def test_root_launchers_do_not_run_offline_repair_before_launch(self):
        shell_code = SHELL_LAUNCHER.read_text(encoding="utf-8")
        cmd_code = CMD_LAUNCHER.read_text(encoding="utf-8")
        for code in [shell_code, cmd_code]:
            self.assertNotIn("echo codex-perf:", code)
            self.assertNotIn("printf '%s\\n' \"codex-perf:", code)
            self.assertNotIn("status --exit-code", code)
            self.assertNotIn("checking whether title repair is needed", code)
            self.assertNotIn("repair is needed; checking for running Codex processes", code)
            self.assertNotIn(" stop -y", code)
            self.assertNotIn("fix-codex-perf.py\" repair", code)
            self.assertNotIn("fix-codex-perf.py repair", code)
            self.assertNotIn("repair -y", code)

    def test_root_launchers_return_without_console_output(self):
        shell_code = SHELL_LAUNCHER.read_text(encoding="utf-8")
        cmd_code = CMD_LAUNCHER.read_text(encoding="utf-8")

        self.assertIn("nohup \"$PYTHON\"", shell_code)
        self.assertIn(">/dev/null 2>&1 &", shell_code)
        self.assertIn("exit 0", shell_code)
        self.assertNotIn("exec \"$PYTHON\"", shell_code)

        self.assertIn("start \"\" /b py -3", cmd_code)
        self.assertIn("start \"\" /b python", cmd_code)
        self.assertIn(">nul 2>nul", cmd_code)
        self.assertIn("exit /b 0", cmd_code)

    def test_no_inject_path_stops_existing_renderer_patch(self):
        outer = self

        class FakeClient:
            def __init__(self):
                self.calls = []

            def call(self, method, params=None, timeout=30.0):
                self.calls.append((method, params or {}))
                if method == "Runtime.evaluate":
                    expression = params["expression"]
                    outer.assertIn("__codexPerfFastLoaderStop", expression)
                    return {"result": {"result": {"value": {"hadPatch": True, "stillInjected": False}}}}
                return {"result": {}}

        client = FakeClient()
        result = self.launcher.stop_existing_renderer_patch(client)
        self.assertTrue(result["hadPatch"])
        self.assertEqual([method for method, _ in client.calls], ["Runtime.enable", "Runtime.evaluate"])

if __name__ == "__main__":
    unittest.main()
