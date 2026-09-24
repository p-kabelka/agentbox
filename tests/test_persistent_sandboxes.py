"""Persistent CLI lifecycle with real flock locks and a simulated Compose provider."""

import asyncio
import io
import json
import runpy
import subprocess
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class PersistentSandboxesTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.g = self.agentbox["_launch"].__globals__
        p = patch.dict(self.g, AGENTBOX_HOME=ROOT, AGENTBOX_STATE=self.root / "state", compose=self.compose,
                       resolve_session=lambda args: (self.session, self.project))
        p.start()
        self.addCleanup(p.stop)
        self.session = self.root / "state" / "sessions" / self.agentbox["_session_id"]("feature", self.project)
        self.session.mkdir(parents=True)
        (self.session / "proxy-config").mkdir()
        (self.session / "proxy-config" / "proxy.yaml").write_text("providers: []\n")
        self.agentbox["generate_compose"](self.session, self.project, "feature", 9876, {}, {}, [], persist=True)
        self.args = types.SimpleNamespace(name="feature", session=None, exec_cmd=[])
        self.events = []
        self.starts = []
        self.git_events = []
        self.containers = {}
        self.start_return = 0
        self.agent_exit = 7
        self.sync_fails = False
        self.stop_fails = False
        self.attaching = None
        self.release_attach = None
        self.compose_hash = None
        p = patch.object(subprocess, "run", side_effect=self.podman)
        p.start()
        self.addCleanup(p.stop)

    def container(self, service, status="created", project=None):
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())["services"][service]
        name = cfg["container_name"]
        labels = {**cfg.get("labels", {}), "com.docker.compose.project": project or self.agentbox["_project"](self.session),
                  "com.docker.compose.service": service}
        c = {"Id": service + "-id", "Name": name,
             "Config": {"Labels": labels, "Image": cfg["image"], "Env": dict(cfg.get("environment", {})),
                        "Cmd": cfg.get("command")},
             "State": {"Status": status, "Running": status == "running", "Pid": 12 if status == "running" else 0,
                       "ExitCode": 0}}
        self.containers[name] = c
        return c

    def compose(self, session, *args, **kwargs):
        self.events.append(args)
        action = args[0]
        if action == "up":
            current_hash = (session / "compose.yaml").read_bytes()
            if self.containers and current_hash != self.compose_hash and "--no-recreate" not in args:
                self.containers.clear()  # podman-compose's default up tears down the project
            if args[-1] == "proxy":
                proxy = self.containers.get(self.agentbox["_persistent_name"](session, "proxy"))
                if proxy is None:
                    proxy = self.container("proxy")
                proxy["State"].update(Status="running", Running=True, Pid=12)
            elif args[-1] == "agent" and "--no-start" in args and "--no-deps" in args:
                self.container("agent")
            else:
                self.fail(f"unexpected Compose up: {args}")
            self.compose_hash = current_hash
        elif action == "exec":
            if self.sync_fails:
                return subprocess.CompletedProcess(args, 1, stdout=b"")
            cfg = yaml.safe_load((session / "proxy-config" / "proxy.yaml").read_text())
            full = self.g["fingerprint"](cfg)
            reply = {"status": 200, "body": {"fingerprint": full,
                    "provider_fingerprint": self.g["fingerprint"](cfg.get("providers", []))}}
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(reply).encode())
        elif action == "stop":
            if self.stop_fails:
                return subprocess.CompletedProcess(args, 1)
            for c in self.containers.values():
                if c["State"]["Running"]:
                    c["State"].update(Status="exited", Running=False, Pid=0, ExitCode=143)
        elif action == "down":
            for c in self.containers.values():
                if c["State"]["Running"]:
                    c["State"].update(Status="exited", Running=False, Pid=0, ExitCode=137)
            self.containers.clear()
        return subprocess.CompletedProcess(args, 0)

    def podman(self, args, **kwargs):
        if args[:2] == ["podman", "ps"]:
            if "-a" in args:
                project = args[args.index("--filter") + 1].rsplit("=", 1)[1]
                containers = [c for c in self.containers.values()
                              if c["Config"]["Labels"].get("com.docker.compose.project") == project]
                if "{{.Names}}" in args[args.index("--format") + 1]:
                    return subprocess.CompletedProcess(args, 0, stdout="\n".join(
                        f"{c['Id']}\t{c['Name']}" for c in containers))
                return subprocess.CompletedProcess(args, 0, stdout="\n".join(c["Id"] for c in containers))
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(
                c["Name"] for c in self.containers.values() if c["State"]["Running"]))
        if args[:3] == ["podman", "inspect", "--format"]:
            state = next(c["State"] for c in self.containers.values() if c["Id"] == args[4])
            if args[3] == "{{.State.ExitCode}}":
                return subprocess.CompletedProcess(args, 0, stdout=f"{state['ExitCode']}\n")
            return subprocess.CompletedProcess(args, 0, stdout=f"{state['Pid']}\t{state['Status']}\n")
        if args[:3] == ["podman", "start", "-ai"]:
            self.starts.append(tuple(args))
            c = self.containers[args[3]]
            c["State"].update(Status="running", Running=True, Pid=12, ExitCode=0)
            if self.attaching:
                self.attaching.set()
                self.release_attach.wait(3)
            if c["State"]["Running"] and self.start_return == 0:
                c["State"].update(Status="exited", Running=False, Pid=0, ExitCode=self.agent_exit)
            return subprocess.CompletedProcess(args, self.start_return or c["State"]["ExitCode"])
        if args[0] == "git":
            self.git_events.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="")
        return subprocess.CompletedProcess(args, 0)

    def start(self, cmd=None):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                self.agentbox["_launch"](self.session, cmd)
        return result.exception.code

    def test_first_and_second_start_reuse_same_service_and_command(self):
        self.assertEqual(self.start(["bash", "-l"]), 7)
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        self.assertEqual(cfg["services"]["agent"]["command"], ["bash", "-l"])
        self.assertNotIn("labels", cfg["services"]["agent"])
        self.assertNotIn("labels", cfg["services"]["proxy"])
        name = cfg["services"]["agent"]["container_name"]
        agent = self.containers[name]
        self.assertEqual(agent["Id"], "agent-id")
        self.assertFalse(agent["State"]["Running"])
        self.assertEqual(self.start(), 7)
        self.assertIs(self.containers[name], agent)
        self.assertEqual([e for e in self.events if e[-1] == "agent"],
                         [("up", "--no-start", "--no-deps", "--no-recreate", "agent")])
        self.assertEqual([e for e in self.events if e[-1] == "proxy"],
                         [("up", "-d", "--no-recreate", "proxy")] * 2)
        self.assertEqual(self.starts, [("podman", "start", "-ai", name)] * 2)
        self.assertEqual([e for e in self.events if e[0] == "stop"], [("stop",)] * 2)
        self.assertFalse(any(e[0] in ("run", "down") for e in self.events))
        self.assertEqual([e[0] for e in self.events], ["up", "exec", "up", "stop", "up", "exec", "stop"])

    def test_first_start_recovers_stopped_proxy_from_failed_creation(self):
        proxy = self.container("proxy", "exited")
        self.compose_hash = (self.session / "compose.yaml").read_bytes()
        self.assertEqual(self.start(["tmux", "new-session"]), 7)
        self.assertIs(self.containers[proxy["Name"]], proxy)
        self.assertEqual([e[0] for e in self.events], ["up", "exec", "up", "stop"])
        self.assertIn(self.agentbox["_persistent_name"](self.session, "agent"), self.containers)

    def test_reuse_rejects_command_before_proxy_changes(self):
        self.start()
        self.events.clear()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.agentbox["_launch"](self.session, ["bash"])
        self.assertEqual(self.events, [])

    def test_agent_compose_edits_do_not_reconfigure_retained_container(self):
        self.start(["bash", "-l"])
        name = self.agentbox["_persistent_name"](self.session, "agent")
        container = self.containers[name]
        saved = dict(container["Config"])
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        cfg["services"]["agent"]["environment"]["CHANGED"] = "yes"
        cfg["services"]["agent"]["image"] = "localhost/changed:latest"
        cfg["services"]["agent"]["command"] = ["other-command"]
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        self.events.clear()
        self.assertEqual(self.start(), 7)
        self.assertIs(self.containers[name], container)
        self.assertEqual(container["Config"], saved)
        self.assertEqual([e[0] for e in self.events], ["up", "exec", "stop"])
        self.assertIn(("up", "-d", "--no-recreate", "proxy"), self.events)

    def test_failed_sync_does_not_create_agent_and_stops_new_proxy(self):
        self.sync_fails = True
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.agentbox["_launch"](self.session)
        self.assertEqual([e[0] for e in self.events], ["up", "exec", "stop"])
        self.assertFalse(self.containers[self.agentbox["_persistent_name"](self.session, "proxy")]["State"]["Running"])

    def test_recovery_refuses_running_unattached_agent(self):
        self.container("agent", "running")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.agentbox["_launch"](self.session)
        self.assertEqual(self.events, [])
        self.agentbox["cmd_stop"](self.args)
        self.assertEqual(self.start(), 7)
        self.assertEqual([e for e in self.events if e[-1] == "agent"], [])

    def test_remove_stops_running_unattached_agent(self):
        self.container("agent", "running")
        self.agentbox["cmd_remove"](self.args)
        self.assertEqual(self.events, [("down", "-v")])
        self.assertFalse(self.session.exists())
        self.assertEqual(self.containers, {})

    def test_fetch_even_if_cleanup_fails(self):
        (self.session / "output.git").mkdir()
        self.stop_fails = True
        self.assertEqual(self.start(), 1)
        self.assertEqual(self.git_events[0][0:5], ["git", "-C", str(self.project), "-c", "core.hooksPath=/dev/null"])
        self.assertEqual(self.git_events[0][5:], ["fetch", "agentbox-feature"])

    def test_container_listing_filters_compose_project(self):
        self.container("agent", project="other-project")
        self.assertEqual(self.agentbox["_list_session_containers"](self.session), [])
        self.containers.clear()
        other = self.container("agent")
        self.assertEqual(self.agentbox["_list_session_containers"](self.session)[0]["name"], other["Name"])

    def test_compose_project_name_is_unique_in_both_modes(self):
        persistent_project = self.agentbox["_project"](self.session)
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        cfg["x-metadata"]["persist"] = False
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        self.assertEqual(self.agentbox["_project"](self.session), persistent_project)

        other_project = self.root / "other" / "project"
        other_project.mkdir(parents=True)
        other_session = self.session.parent / self.agentbox["_session_id"]("feature", other_project)
        other_session.mkdir()
        cfg["x-metadata"]["project-dir"] = str(other_project)
        (other_session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        self.assertNotEqual(self.agentbox["_project"](other_session), persistent_project)

    def test_stop_remove_and_listing_distinguish_retained_agent(self):
        self.start()
        with patch.dict(self.g, find_project_dir=lambda: self.project):
            entry = self.agentbox["_list_sessions"](False)[0]
        self.assertEqual((entry["status"], entry["persistent"], entry["url"]), ("stopped", True, ""))
        self.assertEqual({c["state"] for c in self.agentbox["_list_session_containers"](self.session)}, {"exited"})
        with redirect_stdout(io.StringIO()) as listing:
            self.agentbox["cmd_containers"](types.SimpleNamespace(json=False))
        self.assertIn("STATE", listing.getvalue())
        self.assertIn("exited", listing.getvalue())
        with redirect_stdout(io.StringIO()) as listing:
            self.agentbox["cmd_containers"](types.SimpleNamespace(json=True))
        self.assertEqual(json.loads(listing.getvalue())[0]["state"], "exited")
        self.events.clear()
        self.agentbox["cmd_stop"](self.args)
        self.assertEqual(self.events, [("stop",)])
        self.agentbox["cmd_remove"](self.args)
        self.assertEqual(self.events[-1], ("down", "-v"))
        self.assertFalse(self.session.exists())
        self.assertEqual(self.containers, {})

    def test_running_proxy_alone_not_reported_as_running_session(self):
        self.container("proxy", "running")
        with patch.dict(self.g, find_project_dir=lambda: self.project):
            entry = self.agentbox["_list_sessions"](False)[0]
        self.assertEqual(entry["status"], "stopped")
        self.assertEqual(entry["url"], "")

    def test_active_start_is_exclusive_but_stop_can_interrupt_it(self):
        self.attaching = threading.Event()
        self.release_attach = threading.Event()
        exits = []
        thread = threading.Thread(target=lambda: exits.append(self.start()))
        thread.start()
        self.assertTrue(self.attaching.wait(2))
        self.events.clear()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.agentbox["_launch"](self.session)
        self.assertEqual(self.events, [])
        self.agentbox["cmd_stop"](self.args)
        self.release_attach.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(exits, [143])
        self.assertEqual([e for e in self.events if e[0] == "stop"], [("stop",)] * 2)

    def test_remove_stops_attached_start_and_skips_deleted_session_cleanup(self):
        (self.session / "output.git").mkdir()
        self.attaching = threading.Event()
        self.release_attach = threading.Event()
        exits = []
        thread = threading.Thread(target=lambda: exits.append(self.start()))
        thread.start()
        self.assertTrue(self.attaching.wait(2))
        self.events.clear()
        self.agentbox["cmd_remove"](self.args)
        self.release_attach.set()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(exits, [137])
        self.assertEqual(self.events, [("down", "-v")])
        self.assertFalse(any("fetch" in args for args in self.git_events))
        self.assertFalse(self.session.exists())

    def test_reinit_guard_after_first_creation(self):
        self.start()
        original = (self.session / "compose.yaml").read_bytes()
        init = types.SimpleNamespace(dir=str(self.project), name="feature", persist=True, preset="default", no_git=True,
                                     branch="", ro_mounts=[], rw_mounts=[], start=False)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.agentbox["cmd_init"](init)
        init.persist = False
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.agentbox["cmd_init"](init)
        self.assertEqual((self.session / "compose.yaml").read_bytes(), original)

    def test_init_persist_start_and_old_session_mode_guard(self):
        (self.session / "compose.yaml").unlink()
        init = types.SimpleNamespace(dir=str(self.project), name="feature", persist=True, preset="default", no_git=True,
                                     branch="", ro_mounts=[f"{self.project}:reference"], rw_mounts=[], start=True)
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
            self.agentbox["cmd_init"](init)
        self.assertEqual(result.exception.code, 7)
        self.assertTrue(self.agentbox["_persistent"](self.session))
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        self.assertNotIn("command", cfg["services"]["agent"])
        self.assertIn(f"{self.project}:/context/reference:ro,Z", cfg["services"]["agent"]["volumes"])
        self.containers.clear()
        cfg["x-metadata"].pop("persist")
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        self.assertFalse(self.agentbox["_persistent"](self.session))
        original = (self.session / "compose.yaml").read_bytes()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.agentbox["cmd_init"](init)
        self.assertEqual((self.session / "compose.yaml").read_bytes(), original)

    def test_ephemeral_containers_also_include_state(self):
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        cfg["x-metadata"]["persist"] = False
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        name = "agentbox-project-feature_agent_run_1"
        self.containers[name] = {"Id": "ephemeral-id", "Name": name,
                                 "Config": {"Labels": {"com.docker.compose.project": self.agentbox["_project"](self.session)}},
                                 "State": {"Status": "exited", "Pid": 0, "Running": False}}
        self.assertEqual(self.agentbox["_list_session_containers"](self.session),
                         [{"id": "ephemeral-id", "pid": "0", "name": name, "state": "exited"}])
        with redirect_stdout(io.StringIO()) as listing:
            self.agentbox["cmd_containers"](types.SimpleNamespace(json=False))
        self.assertIn("STATE", listing.getvalue())
        self.assertIn("exited", listing.getvalue())
        with redirect_stdout(io.StringIO()) as listing:
            self.agentbox["cmd_containers"](types.SimpleNamespace(json=True))
        self.assertEqual(json.loads(listing.getvalue())[0]["state"], "exited")

    def test_invalid_metadata_and_failed_start_attach(self):
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        cfg["x-metadata"]["persist"] = "yes"
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.agentbox["_launch"](self.session)
        self.assertEqual(self.events, [])
        cfg["x-metadata"]["persist"] = True
        (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        self.start_return = 125
        self.assertEqual(self.start(), 125)
        self.assertFalse(self.containers[self.agentbox["_persistent_name"](self.session, "agent")]["State"]["Running"])


class PodmanComposeCompatibilityTest(unittest.TestCase):
    def test_up_creates_only_agent_after_proxy_yaml_hash_changes(self):
        try:
            import podman_compose
        except ImportError:
            self.skipTest("Install podman-compose==1.5.0 to verify persistent service creation")

        args = podman_compose.podman_compose._parse_args(
            ["up", "--no-start", "--no-deps", "--no-recreate", "agent"])
        self.assertEqual(args.services, ["agent"])
        self.assertTrue(args.no_start and args.no_deps and args.no_recreate)

        podman = types.SimpleNamespace(
            output=AsyncMock(return_value=json.dumps([{
                "Names": ["proxy"], "Labels": {"io.podman.compose.config-hash": "old"}
            }]).encode()),
            run=AsyncMock(return_value=0),
        )
        compose = types.SimpleNamespace(
            services={"proxy": {"_deps": []}, "agent": {"_deps": []}},
            containers=[{"_service": "proxy", "name": "proxy"},
                        {"_service": "agent", "name": "agent"}],
            yaml_hash="new", project_name="test", podman=podman,
            commands={"build": AsyncMock(return_value=0), "down": AsyncMock()},
        )
        with patch.object(podman_compose, "create_pods", new=AsyncMock()), \
             patch.object(podman_compose, "container_to_args", new=AsyncMock(return_value=["--name", "agent"])):
            self.assertEqual(asyncio.run(podman_compose.compose_up(compose, args)), 0)
        podman.run.assert_awaited_once_with([], "create", ["--name", "agent"])
        compose.commands["down"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
