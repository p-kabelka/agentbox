"""Host transactions and the stdin protocol, using real files and locks."""

import asyncio
import hashlib
import importlib.util
import io
import json
import runpy
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "proxy" / "addons"))
from secret_contract import fingerprint, injection_secret_name, installation_frame, synchronization_frame

spec = importlib.util.spec_from_file_location("manage_secrets", ROOT / "proxy" / "manage_secrets.py")
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class SecretManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name)
        self.patch = patch.object(manager, "SECRET_DIR", self.store)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.target = injection_secret_name("/keys/token")

    def test_verified_atomic_install_and_reset_partial_staging(self):
        manager.install(self.target, io.BytesIO(installation_frame(b"payload\n")))
        target = self.store / self.target
        self.assertEqual(target.read_bytes(), b"payload\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o400)
        (self.store / (manager.TEMP_PREFIX + "interrupted")).write_bytes(b"partial")
        manager.reset()
        self.assertEqual(list(self.store.iterdir()), [])
        manager.install(self.target, io.BytesIO(installation_frame(b"recovered")))
        self.assertEqual(target.read_bytes(), b"recovered")

    def test_bad_frames_and_unwritable_install_leave_no_temporary_file(self):
        valid = installation_frame(b"private-value")
        frames = [b"garbage\n", valid[:-1], valid + b"extra",
                  valid.replace(b"private-value", b"changed-value"), b"0 " + b"a" * 64 + b"\n"]
        for frame in frames:
            with self.subTest(frame=frames.index(frame)), self.assertRaises(ValueError):
                manager.install(self.target, io.BytesIO(frame))
            self.assertEqual(list(self.store.iterdir()), [])
        with patch.object(manager.os, "replace", side_effect=PermissionError), self.assertRaises(OSError):
            manager.install(self.target, io.BytesIO(valid))
        self.assertEqual(list(self.store.iterdir()), [])

    def test_reset_checks_inventory_before_removing_anything(self):
        managed = self.store / self.target
        managed.write_bytes(b"managed")
        unrelated = self.store / "oauth.json"
        unrelated.write_bytes(b"unrelated")
        with self.assertRaises(ValueError):
            manager.reset()
        self.assertTrue(managed.exists())
        self.assertTrue(unrelated.exists())

    def test_symlink_and_invalid_target_refused(self):
        (self.store / self.target).symlink_to("/etc/passwd")
        with self.assertRaises(ValueError):
            manager.reset()
        with self.assertRaises(ValueError):
            manager.install("../escape", io.BytesIO(installation_frame(b"payload")))

    def test_sync_replaces_all_targets_then_reloads_once(self):
        other = injection_secret_name("/keys/other")
        (self.store / self.target).write_bytes(b"old")
        calls = []
        def request(path, **kwargs):
            calls.append((path, kwargs))
            if path == "/health":
                self.assertEqual((self.store / self.target).read_bytes(), b"old")
                return 200, {"ready": True}
            self.assertEqual((self.store / self.target).read_bytes(), b"new")
            self.assertEqual((self.store / other).read_bytes(), b"second")
            return 200, {"fingerprint": "a" * 64}
        frame = synchronization_frame("a" * 64, [(self.target, b"new"), (other, b"second")])
        with patch.object(manager, "_request", side_effect=request):
            self.assertEqual(manager.sync(io.BytesIO(frame)),
                             {"status": 200, "body": {"fingerprint": "a" * 64}})
        self.assertEqual([path for path, _ in calls], ["/health", "/reload/providers"])
        self.assertEqual(calls[1][1]["expected"], "a" * 64)
        self.assertEqual(stat.S_IMODE((self.store / other).stat().st_mode), 0o400)

    def test_sync_rejects_bad_frames_without_reloading(self):
        full = "a" * 64
        valid = synchronization_frame(full, [(self.target, b"secret")])
        frames = [b"invalid\n", valid[:-1], valid + b"extra",
                  valid.replace(b"secret", b"change"),
                  synchronization_frame(full, [(self.target, b"secret"), (self.target, b"secret")]),
                  valid.replace(self.target.encode(), b"../escape")]
        with (patch.object(manager, "wait_ready"), patch.object(manager, "_request") as request):
            for frame in frames:
                with self.subTest(frame=frames.index(frame)), self.assertRaises(ValueError):
                    manager.sync(io.BytesIO(frame))
            request.assert_not_called()
        self.assertEqual(list(self.store.glob(manager.TEMP_PREFIX + "*")), [])

    def test_sync_never_resets_when_proxy_is_unready(self):
        (self.store / self.target).write_bytes(b"old")
        with (patch.object(manager, "_request", side_effect=ConnectionRefusedError) as request,
              patch.object(manager.time, "sleep")):
            with self.assertRaises(ValueError):
                manager.sync(io.BytesIO(synchronization_frame("a" * 64, [])))
        self.assertEqual(request.call_count, 40)
        self.assertEqual((self.store / self.target).read_bytes(), b"old")

    def test_reload_request_forwards_fingerprint_over_loopback(self):
        with patch.object(manager.http.client, "HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 409
            response.read.return_value = b'{"error":"conflict"}'
            self.assertEqual(manager._request("/reload/providers", expected="a" * 64,
                                              timeout=120), (409, {"error": "conflict"}))
            connection.assert_called_once_with("127.0.0.1", manager.RELOAD_PORT, timeout=120)
            connection.return_value.request.assert_called_once_with(
                "GET", "/reload/providers", headers={"X-Agentbox-Config-Fingerprint": "a" * 64})
            connection.return_value.close.assert_called_once()


class HostSecretsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        (self.session / "proxy-config").mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = self.project / "token"
        self.source.write_bytes(b"private-test-value\n")
        self.source.chmod(0o600)
        self.store = self.root / "store"
        self.store.mkdir()
        self.cfg = {"providers": [{"name": "test", "enabled": True, "credential_type": "static",
                                   "api_key_file": "token"}]}
        self.write_config(self.cfg)
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.g = self.agentbox["_launch"].__globals__
        self.g["AGENTBOX_HOME"] = ROOT
        self.agentbox["generate_compose"](self.session, self.project, "test", 1234, self.cfg, {}, [])
        self.args = types.SimpleNamespace()
        self.patches = patch.dict(self.g, resolve_session=lambda args: (self.session, self.project))
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.events = []
        self.frames = []
        self.probes = []

    def write_config(self, cfg):
        (self.session / "proxy-config" / "proxy.yaml").write_text(yaml.safe_dump(cfg))

    def compose(self, session, *args, **kwargs):
        self.events.append(args)
        if args[-1] == "sync":
            self.frames.append(kwargs["input"])
            def request(path, **options):
                self.probes.append((path, options))
                if path == "/health":
                    return 200, {"ready": True}
                cfg = yaml.safe_load((self.session / "proxy-config" / "proxy.yaml").read_text())
                return 200, {"fingerprint": fingerprint(cfg),
                             "provider_fingerprint": fingerprint(cfg.get("providers", [])),
                             "unavailable": []}
            with patch.object(manager, "SECRET_DIR", self.store), patch.object(manager, "_request", side_effect=request):
                try:
                    reply = manager.sync(io.BytesIO(kwargs["input"]))
                except (OSError, ValueError):
                    return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(reply).encode(), stderr=b"")
        return subprocess.CompletedProcess(args, 0, stdout="{}\n200", stderr="")

    def test_discovery_all_scopes_exclusions_dedup_and_project_resolution(self):
        cfg = {"providers": [
            {"name": "disabled", "enabled": False, "api_key_file": "disabled"},
            {"name": "top-level", "enabled": True, "api_key_file": "token"},
            {"name": "scoped", "enabled": True, "api_key_file": "ignored",
             "injection_policy": [{"api_key_file": "nested"}, {"api_key_env": "ONLY_ENV"}],
             "request_policy": [{"host": "[invalid", "injection_policy": [
                 {"api_key_file": "token"}, {"api_key_file": "rule"}]}]},
        ]}
        refs = self.agentbox["discover_secrets"](cfg, self.project)
        self.assertEqual([r.source for r in refs], ["token", "nested", "rule"])
        self.assertEqual([r.provider for r in refs], ["top-level", "scoped", "scoped"])
        self.assertTrue(all(r.host_path.parent == self.project for r in refs))
        with patch.dict(self.g, injection_secret_name=lambda source: "a" * 12 + "-collision"):
            with self.assertRaisesRegex(ValueError, "collision"):
                self.agentbox["discover_secrets"](cfg, self.project)

    def test_stable_open_inode_then_next_command_resolves_symlink_retarget(self):
        original = Path.open
        replacement = self.project / "replacement"
        replacement.write_bytes(b"new-value")
        def replacing_open(path, *args, **kwargs):
            stream = original(path, *args, **kwargs)
            replacement.replace(self.source)
            return stream
        reference = self.agentbox["discover_secrets"](self.cfg, self.project)[0]
        with patch.object(Path, "open", autospec=True, side_effect=replacing_open):
            self.assertEqual(self.agentbox["_read_source"](reference), b"private-test-value\n")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.agentbox["_read_source"](reference), b"new-value")
        link = self.project / "link"
        link.symlink_to(self.source)
        self.cfg["providers"][0]["api_key_file"] = "link"
        first = self.agentbox["discover_secrets"](self.cfg, self.project)[0]
        replacement.write_bytes(b"retargeted")
        link.unlink()
        link.symlink_to(replacement)
        second = self.agentbox["discover_secrets"](self.cfg, self.project)[0]
        self.assertEqual(first.target, second.target)
        self.assertNotEqual(first.host_path, second.host_path)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.agentbox["_read_source"](second), b"retargeted")

    def test_missing_empty_invalid_and_unreadable_sources_are_not_transferred(self):
        directory = self.project / "directory"
        directory.mkdir()
        empty = self.project / "empty"
        empty.write_text(" \n\t\u2003")
        (self.project / "invalid").write_bytes(b"\xff")
        (self.project / "loop").symlink_to("loop")
        for source in ("missing", "directory", "empty", "loop", "invalid"):
            self.cfg["providers"][0]["api_key_file"] = source
            ref = self.agentbox["discover_secrets"](self.cfg, self.project)[0]
            with redirect_stderr(io.StringIO()):
                self.assertIsNone(self.agentbox["_read_source"](ref))
        with patch.object(Path, "open", side_effect=PermissionError), redirect_stderr(io.StringIO()):
            self.assertIsNone(self.agentbox["_read_source"](ref))

    def test_start_transfers_before_launch_without_persistent_state_or_leaks(self):
        output = io.StringIO()
        before = {p.relative_to(self.session) for p in self.session.rglob("*")}
        with patch.dict(self.g, compose=self.compose), redirect_stdout(output), redirect_stderr(output):
            with self.assertRaises(SystemExit) as exit:
                self.agentbox["_launch"](self.session)
        self.assertEqual(exit.exception.code, 0)
        up_index = self.events.index(("up", "-d", "proxy"))
        sync_index = next(i for i, e in enumerate(self.events) if e[-1] == "sync")
        run_index = next(i for i, e in enumerate(self.events) if e[0] == "run")
        self.assertTrue(up_index < sync_index < run_index)
        self.assertEqual([path for path, _ in self.probes], ["/health", "/reload/providers"])
        self.assertIn("--no-deps", self.events[run_index])
        self.assertEqual(self.events[-1], ("down",))
        self.assertEqual(self.frames, [synchronization_frame(fingerprint(self.cfg), [
            (injection_secret_name("token"), self.source.read_bytes())])])
        self.assertEqual((self.store / injection_secret_name("token")).read_bytes(), self.source.read_bytes())
        after = {p.relative_to(self.session) for p in self.session.rglob("*")}
        self.assertEqual(after - before, {Path(".secret-sync.lock"), Path(".lifetime.lock")})
        for text in (output.getvalue(), repr(self.events), (self.session / "compose.yaml").read_text()):
            self.assertNotIn("private-test-value", text)
            self.assertNotIn(hashlib.sha256(self.source.read_bytes()).hexdigest(), text)
        self.assertEqual((self.session / ".secret-sync.lock").read_bytes(), b"")

    def test_secret_synchronization_uses_one_exec_for_all_steps(self):
        full, providers, sources = self.agentbox["_secret_preflight"](self.session)
        with patch.dict(self.g, compose=self.compose):
            self.agentbox["_synchronize_proxy"](self.session, full, providers, sources)
        self.assertEqual(self.events, [("exec", "-T", "proxy", "python3", "/app/manage_secrets.py", "sync")])
        self.assertEqual([path for path, _ in self.probes], ["/health", "/reload/providers"])
        self.assertEqual(self.frames, [synchronization_frame(full, [
            (injection_secret_name("token"), self.source.read_bytes())])])

    def test_multiple_sources_still_use_one_exec_and_skip_missing_files(self):
        (self.project / "second").write_bytes(b"second-key")
        self.cfg["providers"].extend([
            {"name": "second", "enabled": True, "credential_type": "static", "api_key_file": "second"},
            {"name": "missing", "enabled": True, "credential_type": "static", "api_key_file": "absent"},
        ])
        self.write_config(self.cfg)
        with patch.dict(self.g, compose=self.compose), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.agentbox["cmd_proxy_reload"](self.args)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.frames, [synchronization_frame(fingerprint(self.cfg), [
            (injection_secret_name("token"), self.source.read_bytes()),
            (injection_secret_name("second"), b"second-key")])])
        self.assertEqual(len(list(self.store.iterdir())), 2)

    def test_reload_and_restart_always_transfer_current_snapshot_once(self):
        for command in ("cmd_proxy_reload", "cmd_proxy_reload", "cmd_proxy_restart"):
            self.events.clear()
            self.probes.clear()
            with patch.dict(self.g, compose=self.compose), redirect_stdout(io.StringIO()):
                self.agentbox[command](self.args)
            self.assertEqual(sum(e[-1] == "sync" for e in self.events), 1)
            self.assertEqual([path for path, _ in self.probes], ["/health", "/reload/providers"])
            if command == "cmd_proxy_restart":
                self.assertEqual(self.events[0], ("restart", "proxy"))
            else:
                self.assertEqual(len(self.events), 1)
                self.assertEqual(self.events[0][-1], "sync")
                self.assertFalse(any(e[0] in {"up", "restart"} for e in self.events))
        self.cfg["providers"][0]["api_key_file"] = "second"
        (self.project / "second").write_bytes(b"second-key")
        self.write_config(self.cfg)
        with patch.dict(self.g, compose=self.compose), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.agentbox["cmd_proxy_reload"](self.args)
        self.assertEqual(self.frames[-1], synchronization_frame(fingerprint(self.cfg), [
            (injection_secret_name("second"), b"second-key")]))

    def test_preflight_failure_prevents_restart_or_launch(self):
        for text in ("providers: [", "providers: [{enabled: true, api_key_file: '..'}]", "[]"):
            (self.session / "proxy-config" / "proxy.yaml").write_text(text)
            for command in ("cmd_proxy_restart", "cmd_start"):
                self.args.exec_cmd = []
                with patch.dict(self.g, compose=self.compose), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.agentbox[command](self.args)
        self.assertEqual(self.events, [])

    def test_reset_or_transfer_failure_skips_reload_and_agent(self):
        for operation in ("reset", "install"):
            self.events.clear()
            self.probes.clear()
            with (patch.object(manager, operation, side_effect=ValueError),
                  patch.dict(self.g, compose=self.compose), redirect_stderr(io.StringIO()),
                  self.assertRaises(SystemExit)):
                self.agentbox["_launch"](self.session)
            self.assertEqual([path for path, _ in self.probes], ["/health"])
            self.assertFalse(any(e[0] in {"run", "down"} for e in self.events))

    def test_fingerprint_verification_failure_does_not_launch(self):
        def mismatch(session, *args, **kwargs):
            result = self.compose(session, *args, **kwargs)
            if args[-1] == "sync":
                result.stdout = json.dumps({"status": 200, "body": {
                    "fingerprint": "wrong", "provider_fingerprint": "wrong"}}).encode()
            return result
        with (patch.dict(self.g, compose=mismatch),
               redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
            self.agentbox["_launch"](self.session)
        self.assertFalse(any(e[0] in {"run", "down"} for e in self.events))

    def test_synchronized_reload_reports_conflict_and_rejection(self):
        for status, message in ((409, "Concurrent configuration change"),
                                (500, "Proxy rejected the configuration")):
            self.events.clear()
            def rejected(session, *args, **kwargs):
                result = self.compose(session, *args, **kwargs)
                if args[-1] == "sync":
                    reply = json.loads(result.stdout)
                    reply["status"] = status
                    result.stdout = json.dumps(reply).encode()
                return result
            output = io.StringIO()
            with (patch.dict(self.g, compose=rejected), redirect_stderr(output),
                  self.assertRaises(SystemExit)):
                self.agentbox["cmd_proxy_reload"](self.args)
            self.assertIn(message, output.getvalue())
            self.assertEqual(len(self.events), 1)

    def test_environment_only_sync_has_no_transfer_and_build_has_no_secret_directory_side_effect(self):
        self.write_config({"providers": [{"enabled": True, "credential_type": "static", "api_key_env": "KEY"}]})
        with patch.dict(self.g, compose=self.compose), redirect_stdout(io.StringIO()):
            self.agentbox["cmd_proxy_reload"](self.args)
        self.assertEqual(self.frames, [synchronization_frame(fingerprint(yaml.safe_load(
            (self.session / "proxy-config" / "proxy.yaml").read_text())), [])])
        self.assertEqual([path for path, _ in self.probes], ["/health", "/reload/providers"])
        home = self.root / "home"
        (home / "proxy").mkdir(parents=True)
        (home / "proxy" / "Containerfile").touch()
        with (patch.dict(self.g, AGENTBOX_HOME=home),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)),
              redirect_stdout(io.StringIO())):
            self.agentbox["_build_images"](harnesses=["proxy"])
        self.assertFalse((home / "secrets").exists())

    def test_reload_requires_running_proxy(self):
        def stopped(session, *args, **kwargs):
            self.events.append(args)
            return subprocess.CompletedProcess(args, 1)
        with patch.dict(self.g, compose=stopped), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.agentbox["cmd_proxy_reload"](self.args)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][-1], "sync")

    def test_proxy_volumes_are_generated(self):
        config = {"proxy_volumes": [{"src": str(self.source), "dst": "/oauth/key"}]}
        self.agentbox["generate_compose"](self.session, self.project, "test", 1234, config, {}, [])
        self.assertIn("/oauth/key", (self.session / "compose.yaml").read_text())

    def test_all_lifecycle_commands_wait_for_sync_lock(self):
        # Separate file descriptions exercise flock, including threads in one CLI test process.
        for command in ("cmd_start", "cmd_proxy_reload", "cmd_proxy_restart", "cmd_stop", "cmd_remove"):
            self.args.exec_cmd = []
            self.events.clear()
            started, finished = threading.Event(), threading.Event()
            errors = []
            def invoke():
                started.set()
                try:
                    self.agentbox[command](self.args)
                except SystemExit as exc:
                    if exc.code:
                        errors.append(exc)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finished.set()
            with (patch.dict(self.g, compose=self.compose),
                  patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)),
                  redirect_stdout(io.StringIO())):
                with self.agentbox["_secret_sync_lock"](self.session):
                    thread = threading.Thread(target=invoke)
                    thread.start()
                    self.assertTrue(started.wait(2))
                    self.assertFalse(finished.wait(0.05))
                    self.assertEqual(self.events, [])
                thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])

    def test_final_lifetime_teardown_waits_for_sync_lock(self):
        context = self.agentbox["_proxy_lifetime"](self.session)
        with self.agentbox["_secret_sync_lock"](self.session):
            context.__enter__()
        with patch.dict(self.g, compose=self.compose):
            with self.agentbox["_secret_sync_lock"](self.session):
                thread = threading.Thread(target=lambda: context.__exit__(None, None, None))
                thread.start()
                thread.join(0.05)
                self.assertTrue(thread.is_alive())
                self.assertEqual(self.events, [])
            thread.join(2)
            self.assertEqual(self.events, [("down",)])

    def test_allow_saved_conflict_never_synchronizes(self):
        def conflict(session, *args, **kwargs):
            self.events.append(args)
            return subprocess.CompletedProcess(args, 0, stdout='{}\n409')
        output = io.StringIO()
        with patch.dict(self.g, compose=conflict), redirect_stdout(output), redirect_stderr(output), self.assertRaises(SystemExit):
            self.agentbox["_update_allowlist"]("new.example", True, self.args)
        self.assertIn("saved but not active", output.getvalue())
        self.assertIn("proxy-reload", output.getvalue())
        cfg = yaml.safe_load((self.session / "proxy-config" / "proxy.yaml").read_text())
        self.assertEqual(cfg["extra_request_policy"], [{"host": r"new\.example"}])
        self.assertEqual(len(self.events), 1)
        self.assertTrue(self.events[0][-1].endswith("/reload"))


class ComposeCompatibilityTest(unittest.TestCase):
    def test_podman_compose_tmpfs_argument(self):
        try:
            import podman_compose
        except ImportError:
            self.skipTest("Install podman-compose==1.5.0 to verify Compose argument generation")
        service = yaml.safe_load((ROOT / "compose-base.yaml").read_text())["services"]["proxy"]
        service = {"name": "test", "image": service["image"], "tmpfs": service["tmpfs"]}
        compose = types.SimpleNamespace(dirname=str(ROOT), environ={})
        with (patch.object(podman_compose, "assert_cnt_nets", new=AsyncMock()),
              patch.object(podman_compose, "get_net_args", return_value=[])):
            args = asyncio.run(podman_compose.container_to_args(compose, service))
        self.assertEqual(args[args.index("--tmpfs") + 1],
                         "/run/secrets:rw,noexec,nosuid,nodev,mode=0700")


if __name__ == "__main__":
    unittest.main()
