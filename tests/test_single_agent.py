"""One agent owns a session from startup through teardown, in either mode."""

import io
import runpy
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class SingleAgentTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session = self.make_session("feature")
        self.agentbox = runpy.run_path(str(ROOT / "bin/agentbox"))
        self.g = self.agentbox["_launch"].__globals__
        self.events = []
        self.preflight = Mock(return_value=("config", "providers", []))
        self.synchronize = Mock()
        self.network = Mock()
        self.running = Mock(return_value=False)
        p = patch.dict(self.g, compose=self.compose, _running_agent=self.running,
                       _secret_preflight=self.preflight, _synchronize_proxy=self.synchronize,
                       _prepare_agent_network=self.network)
        p.start()
        self.addCleanup(p.stop)

    def make_session(self, name):
        session = self.root / name
        session.mkdir()
        config = yaml.safe_load((ROOT / "compose-base.yaml").read_text())
        config["x-metadata"] = {"name": name, "project-dir": str(self.root), "persist": False}
        (session / "compose.yaml").write_text(yaml.safe_dump(config))
        return session

    def compose(self, session, *args, **kwargs):
        self.events.append(args)
        return subprocess.CompletedProcess(args, 0)

    def start(self):
        with self.assertRaises(SystemExit) as result:
            self.agentbox["_launch"](self.session)
        return result.exception.code

    def test_second_start_fails_before_preflight_or_side_effects_in_both_modes(self):
        for persistent in (False, True):
            cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
            cfg["x-metadata"]["persist"] = persistent
            (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
            with self.agentbox["_agent_lifetime"](self.session):
                self.running.reset_mock()
                with redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(self.start(), 1)
                self.assertIn("session already running", error.getvalue())
                self.running.assert_not_called()
                self.preflight.assert_not_called()
                self.synchronize.assert_not_called()
                self.network.assert_not_called()
                self.assertEqual(self.events, [])

    def test_running_unattached_agent_blocks_start_without_side_effects(self):
        self.running.return_value = True
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.start(), 1)
        self.assertIn("agentbox stop", error.getvalue())
        self.preflight.assert_not_called()
        self.assertEqual(self.events, [])

    def test_different_sessions_have_independent_ownership(self):
        other = self.make_session("other")
        with self.agentbox["_agent_lifetime"](self.session):
            with self.agentbox["_agent_lifetime"](other):
                pass

    def test_start_derives_mode_and_project_from_one_metadata_read_under_sync_lock(self):
        sync_lock = self.g["_secret_sync_lock"]
        locked = False

        @contextmanager
        def lock(*args, **kwargs):
            nonlocal locked
            with sync_lock(*args, **kwargs) as exists:
                locked = True
                try:
                    yield exists
                finally:
                    locked = False

        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                metadata = {**self.agentbox["read_compose_metadata"](self.session), "persist": persistent}

                def read(session):
                    self.assertTrue(locked, "Metadata was read outside the synchronization lock")
                    return metadata

                read_metadata = Mock(side_effect=read)
                mode = Mock(wraps=self.agentbox["_persistent"])
                project = Mock(wraps=self.agentbox["_project_name"])
                with patch.dict(self.g, _secret_sync_lock=lock, read_compose_metadata=read_metadata,
                                _persistent=mode, _project_name=project,
                                _list_session_containers=lambda project: [],
                                _create_persistent_agent=Mock(), _run_persistent_agent=Mock(return_value=0)):
                    self.assertEqual(self.start(), 0)
                read_metadata.assert_called_once_with(self.session)
                mode.assert_called_once_with(metadata)
                project.assert_called_once_with(self.session, metadata)

    def test_failed_start_releases_ownership_for_retry(self):
        self.preflight.side_effect = SystemExit(1)
        self.assertEqual(self.start(), 1)
        self.assertEqual(self.events, [])
        self.preflight.side_effect = None
        self.assertEqual(self.start(), 0)
        self.assertEqual(self.events[-1], ("down",))

    def test_ephemeral_agent_has_a_fixed_name_and_releases_ownership_after_cleanup(self):
        project = self.agentbox["_project_name"](self.session, self.agentbox["read_compose_metadata"](self.session))
        self.assertEqual(self.start(), 0)
        self.assertIn(("run", "--rm", "--no-deps", "--name", f"{project}_agent", "agent"), self.events)
        self.assertEqual(self.events[-1], ("down",))
        self.assertEqual(self.start(), 0)

    def test_teardown_keeps_exclusive_ownership_until_it_finishes(self):
        def compose(session, *args, **kwargs):
            self.events.append(args)
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                with self.agentbox["_agent_lifetime"](self.session):
                    self.fail("Ownership was released before teardown")
            return subprocess.CompletedProcess(args, 0)

        with patch.dict(self.g, compose=compose), self.agentbox["_agent_lifetime"](self.session):
            with self.agentbox["_ephemeral_lifetime"](self.session):
                pass
        self.assertEqual(self.events, [("down",)])

    def test_stop_can_interrupt_an_active_ephemeral_agent(self):
        attached, release = threading.Event(), threading.Event()
        exits = []
        errors = []

        def compose(session, *args, **kwargs):
            self.events.append(args)
            if args[0] == "run":
                attached.set()
                if not release.wait(5):
                    raise AssertionError("Stop could not interrupt the active agent")
                return subprocess.CompletedProcess(args, 143)
            if args[0] == "down":
                release.set()
            return subprocess.CompletedProcess(args, 0)

        def start():
            try:
                exits.append(self.start())
            except BaseException as exc:
                errors.append(exc)

        with patch.dict(self.g, compose=compose, resolve_session=lambda args: (self.session, self.root)):
            thread = threading.Thread(target=start)
            thread.start()
            try:
                self.assertTrue(attached.wait(3))
                self.agentbox["cmd_stop"](None)
            finally:
                release.set()
                thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(exits, [143])
        self.assertEqual(self.events[-2:], [("down",), ("down",)])

    def test_removal_while_attached_skips_teardown(self):
        def remove(session, *args, **kwargs):
            self.events.append(args)
            if args[0] == "run":
                shutil.rmtree(session)
            return subprocess.CompletedProcess(args, 0)

        with patch.dict(self.g, compose=remove):
            self.assertEqual(self.start(), 0)
        self.assertFalse(any(args[0] == "down" for args in self.events))

    def test_running_agent_query_is_scoped_to_the_session_and_agent_service(self):
        project = self.agentbox["_project_name"](self.session, self.agentbox["read_compose_metadata"](self.session))
        # The unpatched function lives in the original run_path mapping.
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="agent-id\n")) as run:
            self.assertTrue(self.agentbox["_running_agent"](self.session))
        run.assert_called_once_with(
            ["podman", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
             "--filter", "label=com.docker.compose.service=agent"],
            capture_output=True, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
