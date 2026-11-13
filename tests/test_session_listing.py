"""Session listing reuses one parsed Compose document per session."""

import copy
import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class SessionListingTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.other_project = self.root / "other-project"
        self.other_project.mkdir()
        self.state = self.root / "state"
        self.sessions = self.state / "sessions"
        self.sessions.mkdir(parents=True)
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.g = self.agentbox["main"].__globals__
        env = patch.dict(self.g, AGENTBOX_STATE=self.state, find_project_dir=lambda: self.project)
        env.start()
        self.addCleanup(env.stop)
        self.base = yaml.safe_load((ROOT / "compose-base.yaml").read_text())
        self.containers = {}

    def session(self, name, *, project=None, persistent=False, agent=None, proxy=None, port=9876):
        project = project or self.project
        session = self.sessions / self.agentbox["_session_id"](name, project)
        session.mkdir()
        cfg = copy.deepcopy(self.base)
        meta = {"name": name, "project-dir": str(project), "persist": persistent}
        cfg["x-metadata"] = meta
        cfg["services"]["proxy"]["ports"] = [f"127.0.0.1:{port}:8081"] if port else []
        (session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        project_name = self.agentbox["_project_name"](session, meta)
        containers = self.containers[project_name] = []
        for service, state in (("agent", agent), ("proxy", proxy)):
            if state is not None:
                name = (self.agentbox["_persistent_name"](session, service) if persistent
                        else f"{project_name}_{service}_1")
                containers.append({"id": name + "-id", "name": name, "state": state,
                                   "pid": "42" if state == "running" else "0"})
        return session

    def podman(self, args, **kwargs):
        if args[:2] == ["podman", "ps"]:
            if "-a" in args:
                project = args[args.index("--filter") + 1].rsplit("=", 1)[1]
                output = "\n".join(f"{c['id']}\t{c['name']}" for c in self.containers.get(project, []))
            else:
                output = "\n".join(c["name"] for group in self.containers.values()
                                   for c in group if c["state"] == "running")
        elif args[:3] == ["podman", "inspect", "--format"]:
            container = next(c for group in self.containers.values() for c in group if c["id"] == args[-1])
            output = f"{container['pid']}\t{container['state']}"
        else:
            self.fail(f"Unexpected command: {args}")
        return subprocess.CompletedProcess(args, 0, stdout=output)

    def listing(self, show_all):
        loader = Mock(wraps=self.g["load_yaml"])
        with (patch.dict(self.g, load_yaml=loader),
              patch.object(subprocess, "run", side_effect=self.podman) as run):
            rows = self.agentbox["_list_sessions"](show_all)
        return rows, Counter(call.args[0] for call in loader.call_args_list), run.call_args_list

    def test_all_sessions_parse_each_compose_once_in_both_modes(self):
        paths = []
        for i in range(13):
            paths.append(self.session(f"feature-{i:02d}", persistent=i < 2,
                                      agent="running" if i % 2 else "exited", proxy="running"))
        rows, reads, calls = self.listing(True)
        self.assertEqual(reads, Counter({session / "compose.yaml": 1 for session in paths}))
        self.assertEqual(len(rows), 13)
        for i, row in enumerate(rows):
            self.assertEqual(row["persistent"], i < 2)
            self.assertEqual(row["project"], str(self.project))
            active = i >= 2 or bool(i % 2)
            self.assertEqual(row["status"], "running" if active else "stopped")
            self.assertEqual(row["url"], "http://localhost:9876" if active else "")
        # This change removes duplicate parsing, not runtime-state queries.
        self.assertEqual(len(calls), 7)

    def test_current_project_filters_without_a_second_parse(self):
        a = self.session("a", agent="running")
        ab = self.session("a-b", persistent=True, agent="running", proxy="exited")
        other = self.session("a", project=self.other_project, persistent=True, agent="running", proxy="running")
        rows, reads, calls = self.listing(False)
        self.assertEqual(reads, Counter({session / "compose.yaml": 1 for session in (a, ab, other)}))
        self.assertEqual([row["name"] for row in rows], ["a", "a-b"])
        self.assertTrue(all("project" not in row for row in rows))
        self.assertEqual(rows[0]["url"], "http://localhost:9876")
        self.assertEqual(rows[1]["status"], "running")
        self.assertEqual(rows[1]["url"], "")
        self.assertEqual(len(calls), 4)

    def test_uninitialized_directories_and_missing_metadata_are_skipped(self):
        self.session("feature", agent="running", port=None)
        (self.sessions / "partial").mkdir()
        empty = self.sessions / "empty"
        empty.mkdir()
        (empty / "compose.yaml").write_text("services: {}\n")
        (self.sessions / "not-a-directory").touch()
        rows, reads, _ = self.listing(True)
        self.assertEqual([row["name"] for row in rows], ["feature"])
        self.assertEqual(rows[0]["url"], "")
        self.assertEqual(sum(reads.values()), 2)
        self.assertTrue(all(count == 1 for count in reads.values()))

    def test_each_listing_observes_fresh_configuration(self):
        session = self.session("feature", agent="running")
        self.assertEqual(self.listing(True)[0][0]["url"], "http://localhost:9876")
        cfg = yaml.safe_load((session / "compose.yaml").read_text())
        cfg["services"]["proxy"]["ports"] = ["127.0.0.1:12345:8081"]
        (session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        rows, reads, _ = self.listing(True)
        self.assertEqual(rows[0]["url"], "http://localhost:12345")
        self.assertEqual(reads, Counter({session / "compose.yaml": 1}))

    def test_invalid_persistence_still_fails(self):
        session = self.session("feature")
        cfg = yaml.safe_load((session / "compose.yaml").read_text())
        cfg["x-metadata"]["persist"] = "false"
        (session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.listing(True)

    def test_list_and_ls_json_use_the_same_single_parse(self):
        session = self.session("feature", persistent=True, agent="exited", proxy="exited")
        for command in ("list", "ls"):
            for flags in ([], ["--all"]):
                with (self.subTest(command=command, flags=flags),
                      patch.object(sys, "argv", ["agentbox", command, *flags, "--json"]),
                      patch.object(subprocess, "run", side_effect=self.podman),
                      redirect_stdout(io.StringIO()) as output):
                    loader = Mock(wraps=self.g["load_yaml"])
                    with patch.dict(self.g, load_yaml=loader):
                        self.agentbox["main"]()
                    self.assertEqual(loader.call_count, 1)
                    loader.assert_called_with(session / "compose.yaml")
                    row = json.loads(output.getvalue())[0]
                    self.assertEqual(row["session_id"], session.name)
                    self.assertEqual(row["status"], "stopped")
                    self.assertEqual("project" in row, bool(flags))


if __name__ == "__main__":
    unittest.main()
