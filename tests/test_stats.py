"""Session resource snapshots, with projected Podman records and real CLI integration."""

import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class StatsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.g = self.agentbox["_collect_stats"].__globals__
        self.project = self.root / "project"
        self.project.mkdir()
        p = patch.dict(self.g, AGENTBOX_STATE=self.root / "state", find_project_dir=lambda: self.project)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(self.g["time"], "sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)
        p = patch.object(subprocess, "run", side_effect=self.podman)
        p.start()
        self.addCleanup(p.stop)
        self.entries = []
        self.measurements = {}
        self.sizes = {}
        self.states = {}
        self.removed = set()
        self.size_errors = set()
        self.discovery_errors = set()
        self.omit_stats = set()
        self.bad_output = {}
        self.calls = []
        self.sample = 0

    def session(self, name="feature", project=None, persistent=False):
        project = project or self.project
        session_dir = self.g["AGENTBOX_STATE"] / "sessions" / self.agentbox["_session_id"](name, project)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "compose.yaml").write_text(yaml.safe_dump({
            "x-metadata": {"name": name, "project-dir": str(project), "persist": persistent},
            "services": {"proxy": {"ports": ["127.0.0.1:9876:8081"]}},
        }))
        return session_dir

    def container(self, session, service="agent", state="running", cpu=25.0, memory=1024, disk=4096, name=None):
        cid = f"{len(self.entries) + 1:064x}"
        entry = {"id": cid, "name": name or f"container-{len(self.entries)}", "service": service,
                 "state": state, "pid": 123 if state in ("running", "paused") else 0,
                 "project": self.agentbox["_project_name"](session)}
        self.entries.append(entry)
        self.measurements[cid] = {"MemUsage": memory, "CPUNano": int(cpu * 10_000_000)}
        self.sizes[cid] = disk
        self.states[cid] = state
        return cid

    def podman(self, args, **kwargs):
        self.calls.append(args)
        self.assertEqual(args[0], "podman")
        records, status = [], 0
        if args[1] == "ps":
            self.assertIn("--no-trunc", args)
            self.assertIn("-a", args)
            project = args[args.index("--filter") + 1].rsplit("=", 1)[1]
            records = [dict(e) for e in self.entries if e["project"] == project]
            status = 125 if project in self.discovery_errors else 0
            kind = "ps"
        elif args[1] == "stats":
            self.assertIn("--no-stream", args)
            self.assertEqual(args[args.index("--format") + 1], "{{json .ContainerStats}}")
            ids = args[args.index("--format") + 2:]
            self.assertTrue(ids)
            self.sample += 1
            for cid in ids:
                self.assertIn(cid, self.measurements)
                if cid in self.omit_stats:
                    continue
                record = {"ContainerID": cid, "SystemNano": 1_000_000_000_000_000_000 + self.sample * 1_000_000_000,
                          "CPUNano": 1_000_000_000_000, "MemUsage": self.measurements[cid]["MemUsage"]}
                if self.sample % 2 == 0:
                    record["CPUNano"] += self.measurements[cid]["CPUNano"]
                records.append(record)
            kind = "stats"
        elif args[1:3] == ["container", "inspect"]:
            ids = args[args.index("--format") + 2:]
            self.assertTrue(ids)
            kind = "size" if "--size" in args else "state"
            for cid in ids:
                if cid in self.removed or kind == "size" and cid in self.size_errors:
                    status = 125
                    continue
                record = {"id": cid, "state": self.states[cid]}
                if kind == "size":
                    record["disk_bytes"] = self.sizes[cid]
                records.append(record)
        elif args[1:3] == ["container", "exists"]:
            return subprocess.CompletedProcess(args, 1 if args[3] in self.removed else 125, stdout="", stderr="")
        else:
            self.fail(f"Unexpected or non-read-only command: {args}")
        output = self.bad_output.get(kind, "\n".join(json.dumps(r) for r in records))
        return subprocess.CompletedProcess(args, status, stdout=output, stderr="private backend details")

    def command(self, *argv):
        with patch.object(sys, "argv", ["agentbox", "stats", *argv]), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            try:
                self.agentbox["main"]()
                status = 0
            except SystemExit as exc:
                status = exc.code
        return status, out.getvalue(), err.getvalue()

    def snapshot(self):
        return self.agentbox["_collect_stats"]()

    def test_global_sessions_exact_membership_and_aggregation(self):
        a = self.session()
        b = self.session(project=self.root / "other", persistent=True)
        orphan = self.root / "orphan"
        orphan.mkdir()
        (orphan / "compose.yaml").write_text(yaml.safe_dump({"x-metadata": {"name": "orphan", "project-dir": str(self.project)}}))
        self.container(a, "proxy", cpu=2.123456, memory=33554432, disk=8388608)
        self.container(a, cpu=138.25, memory=536870912, disk=536870912)
        self.container(a, cpu=0.5, memory=1024, disk=500)
        self.container(a, "extra", state="paused", cpu=0, memory=5000, disk=100)
        self.container(a, None, state="created", disk=5)
        self.container(b, state="exited", disk=256)
        self.container(orphan, cpu=900)
        self.entries.append(dict(self.entries[0]))  # repeated discovery row
        self.entries.append({**self.entries[0], "project": "unrelated-compose-project"})
        snapshot = self.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertIsNotNone(datetime.fromisoformat(snapshot["collected_at"]).tzinfo)
        self.assertEqual(len(snapshot["sessions"]), 2)
        sessions = {s["session_id"]: s for s in snapshot["sessions"]}
        self.assertEqual(len(sessions[a.name]["containers"]), 5)
        totals = sessions[a.name]["totals"]
        self.assertAlmostEqual(totals["cpu_percent"], 140.873456)
        self.assertEqual(totals["memory_bytes"], 33554432 + 536870912 + 1024 + 5000)
        self.assertEqual(totals["disk_bytes"], 8388608 + 536870912 + 500 + 100 + 5)
        self.assertEqual(sessions[b.name]["totals"], {"cpu_percent": 0.0, "memory_bytes": 0, "disk_bytes": 256})
        self.assertTrue(sessions[b.name]["persistent"])
        self.sleep.assert_called_once_with(1)
        samples = [c for c in self.calls if c[1] == "stats"]
        self.assertEqual(len(samples), 2)
        self.assertEqual(len(samples[0][samples[0].index("--format") + 2:]), 4)
        self.assertEqual(len([c for c in self.calls if "--size" in c]), 1)
        self.assertNotIn("pid", sessions[a.name]["containers"][0])

    def test_global_default_does_not_auto_select_local_session(self):
        self.session()
        self.session(project=self.root / "other")
        status, out, err = self.command("--json")
        self.assertEqual(status, 0)
        self.assertEqual(len(json.loads(out)["sessions"]), 2)
        self.assertEqual(err, "")

    def test_explicit_selectors_measure_only_one_session(self):
        local = self.session()
        other = self.session(project=self.root / "other")
        local_id = self.container(local)
        other_id = self.container(other)
        for argv, expected in ((("--name", "feature"), local_id), (("-n", "feature"), local_id),
                               (("--session", other.name), other_id), (("-s", other.name), other_id)):
            with self.subTest(argv=argv):
                self.calls.clear()
                status, out, _ = self.command(*argv, "--json")
                self.assertEqual(status, 0)
                self.assertEqual(json.loads(out)["sessions"][0]["containers"][0]["id"], expected)
                self.assertEqual(len([c for c in self.calls if c[1] == "ps"]), 1)
                self.assertTrue(all(c[-1] == expected for c in self.calls if c[1] == "stats"))

    def test_invalid_selectors_fail_before_output_or_backend_calls(self):
        for argv, code in ((("--name", "missing"), 1), (("--session", "missing"), 1),
                           (("--name", "x", "--session", "y"), 2), (("--session", ".."), 1),
                           (("--name", ""), 1), (("--session", ""), 1),
                           (("--no-stream",), 2)):
            with self.subTest(argv=argv):
                status, out, err = self.command(*argv, "--json")
                self.assertEqual(status, code)
                self.assertEqual(out, "")
                self.assertTrue(err)
        self.assertEqual(self.calls, [])

    def test_unreadable_registry_has_structured_error(self):
        with patch.dict(self.g, _session_dirs=lambda: (_ for _ in ()).throw(PermissionError())):
            status, out, err = self.command("--json")
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(out)["errors"][0]["session_id"], None)
        self.assertIn("registry", err)

    def test_conflicting_project_identities_are_not_counted_twice(self):
        self.session()
        self.session(project=self.root / "other")
        with patch.dict(self.g, _project_name=lambda *args: "same-project"):
            snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertEqual(len(snapshot["errors"]), 2)
        self.assertTrue(all(s["totals"]["disk_bytes"] is None for s in snapshot["sessions"]))
        self.assertEqual(self.calls, [])

    def test_session_removed_between_metadata_and_discovery(self):
        session = self.session()
        metadata = self.g["_session_info"]

        def remove_after_read(path):
            info = metadata(path)
            (path / "compose.yaml").unlink()
            path.rmdir()
            return info

        with patch.dict(self.g, _session_info=remove_after_read):
            snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["errors"][0]["session_id"], session.name)
        self.assertTrue(all(v is None for v in snapshot["sessions"][0]["totals"].values()))

    def test_empty_registry_does_not_require_podman(self):
        self.assertEqual(self.command(), (0, "No sessions.\n", ""))
        status, out, _ = self.command("--json")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out)["sessions"], [])
        self.assertEqual(self.calls, [])

    def test_empty_session_is_not_missing_discovery(self):
        self.session()
        snapshot = self.snapshot()
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["sessions"][0]["totals"], {"cpu_percent": 0.0, "memory_bytes": 0, "disk_bytes": 0})
        self.assertFalse(any(c[1] == "stats" or "--size" in c for c in self.calls))
        _, out, _ = self.command()
        self.assertIn("(no containers)", out)
        self.assertIn("TOTAL", out)

    def test_stopped_containers_keep_disk_without_live_sampling(self):
        session = self.session(persistent=True)
        self.container(session, state="exited", disk=8192)
        self.container(session, "proxy", state="stopped", disk=1)
        snapshot = self.snapshot()
        self.assertTrue(snapshot["complete"])
        for c in snapshot["sessions"][0]["containers"]:
            self.assertIsNone(c["cpu_percent"])
            self.assertIsNone(c["memory_bytes"])
        self.assertEqual(snapshot["sessions"][0]["totals"]["disk_bytes"], 8193)
        self.assertFalse(any(c[1] == "stats" for c in self.calls))
        self.sleep.assert_not_called()

    def test_missing_disk_invalidates_only_disk_total(self):
        session = self.session()
        cid = self.container(session)
        self.container(session, disk=10)
        self.size_errors.add(cid)
        status, out, err = self.command("--json")
        snapshot = json.loads(out)
        self.assertEqual(status, 1)
        self.assertFalse(snapshot["complete"])
        totals = snapshot["sessions"][0]["totals"]
        self.assertIsNone(totals["disk_bytes"])
        self.assertEqual(totals["cpu_percent"], 50)
        self.assertEqual(totals["memory_bytes"], 2048)
        self.assertEqual(snapshot["errors"][0]["metric"], "disk_bytes")
        self.assertNotIn("private backend details", out + err)

    def test_invalid_metric_values_are_not_zero(self):
        for value in (None, -1, "1 MiB", True, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.entries.clear()
                session = self.session()
                cid = self.container(session, memory=value)
                self.sizes[cid] = value
                snapshot = self.snapshot()
                self.assertFalse(snapshot["complete"])
                total = snapshot["sessions"][0]["totals"]
                self.assertIsNone(total["memory_bytes"])
                self.assertIsNone(total["disk_bytes"])
                self.assertEqual(total["cpu_percent"], 25)
                json.dumps(snapshot, allow_nan=False)

    def test_cpu_counter_reset_is_unavailable_but_memory_is_retained(self):
        session = self.session()
        cid = self.container(session)
        self.measurements[cid]["CPUNano"] = -1
        snapshot = self.snapshot()
        self.assertIsNone(snapshot["sessions"][0]["totals"]["cpu_percent"])
        self.assertEqual(snapshot["sessions"][0]["totals"]["memory_bytes"], 1024)
        self.assertFalse(snapshot["complete"])

    def test_exit_during_collection_uses_current_nonlive_state(self):
        session = self.session()
        cid = self.container(session)
        self.states[cid] = "exited"
        snapshot = self.snapshot()
        self.assertTrue(snapshot["complete"])
        c = snapshot["sessions"][0]["containers"][0]
        self.assertEqual(c["state"], "exited")
        self.assertIsNone(c["memory_bytes"])
        self.assertEqual(snapshot["sessions"][0]["totals"], {"cpu_percent": 0, "memory_bytes": 0, "disk_bytes": 4096})

    def test_disappearing_container_retains_identity_and_unknown_totals(self):
        session = self.session()
        cid = self.container(session, state="exited")
        self.removed.add(cid)
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["sessions"][0]["containers"][0]["id"], cid)
        self.assertEqual(snapshot["sessions"][0]["containers"][0]["state"], "exited")
        self.assertTrue(all(v is None for v in snapshot["sessions"][0]["totals"].values()))
        self.assertEqual(snapshot["errors"][0]["code"], "container_disappeared")

    def test_unknown_state_keeps_disk_but_live_totals_unknown(self):
        session = self.session()
        self.container(session, state="removing")
        snapshot = self.snapshot()
        total = snapshot["sessions"][0]["totals"]
        self.assertIsNone(total["cpu_percent"])
        self.assertIsNone(total["memory_bytes"])
        self.assertEqual(total["disk_bytes"], 4096)
        self.assertFalse(snapshot["complete"])

    def test_partial_discovery_failure_does_not_zero_or_hide_session(self):
        a = self.session()
        b = self.session(project=self.root / "other")
        self.container(a)
        self.discovery_errors.add(self.agentbox["_project_name"](b))
        snapshot = self.snapshot()
        sessions = {s["session_id"]: s for s in snapshot["sessions"]}
        self.assertEqual(sessions[a.name]["totals"]["cpu_percent"], 25)
        self.assertTrue(all(v is None for v in sessions[b.name]["totals"].values()))
        self.assertFalse(snapshot["complete"])

    def test_partial_discovery_retains_successful_container_rows(self):
        session = self.session()
        cid = self.container(session)
        self.discovery_errors.add(self.agentbox["_project_name"](session))
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["sessions"][0]["containers"][0]["id"], cid)
        self.assertEqual(snapshot["sessions"][0]["containers"][0]["memory_bytes"], 1024)
        self.assertTrue(all(v is None for v in snapshot["sessions"][0]["totals"].values()))

    def test_invalid_metadata_is_reported_globally_and_fatal_when_selected(self):
        session = self.session()
        for content in ("x-metadata: [bad]", "[not-a-compose-mapping]", "x-metadata: {", "{}",
                        yaml.safe_dump({"x-metadata": {"name": "feature", "project-dir": str(self.project), "persist": "yes"}})):
            with self.subTest(content=content):
                (session / "compose.yaml").write_text(content)
                status, out, _ = self.command("--json")
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(out)["errors"][0]["session_id"], session.name)
                status, out, _ = self.command("--name", "feature", "--json")
                self.assertEqual(status, 1)
                self.assertEqual(out, "")
        self.assertEqual(self.calls, [])

    def test_podman_absent_returns_structured_partial_report(self):
        self.session()
        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            status, out, err = self.command("--json")
        self.assertEqual(status, 1)
        self.assertFalse(json.loads(out)["complete"])
        self.assertIn("Podman", err)

    def test_malformed_output_does_not_become_empty_success(self):
        session = self.session()
        self.container(session)
        for kind in ("ps", "stats", "size"):
            with self.subTest(kind=kind):
                self.bad_output = {kind: "not json"}
                snapshot = self.snapshot()
                self.assertFalse(snapshot["complete"])
                self.assertTrue(snapshot["errors"])

    def test_malformed_current_state_does_not_reuse_stale_stopped_state(self):
        session = self.session()
        cid = self.container(session, state="exited")
        self.states[cid] = None
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        total = snapshot["sessions"][0]["totals"]
        self.assertIsNone(total["cpu_percent"])
        self.assertIsNone(total["memory_bytes"])
        self.assertEqual(total["disk_bytes"], 4096)

    def test_missing_stats_preserves_other_container_measurements(self):
        session = self.session()
        cid = self.container(session)
        self.container(session, memory=9000)
        self.omit_stats.add(cid)
        snapshot = self.snapshot()
        self.assertFalse(snapshot["complete"])
        self.assertIsNone(snapshot["sessions"][0]["totals"]["memory_bytes"])
        self.assertEqual(snapshot["sessions"][0]["containers"][1]["memory_bytes"], 9000)

    def test_table_grouping_rounding_and_control_character_escaping(self):
        session = self.session(name="feature\n\x1b[2J")
        self.container(session, cpu=0.004, memory=1024, disk=1024, name="first\n\x1b[2J")
        self.container(session, cpu=0.004, memory=1024, disk=1024, name="second")
        status, out, err = self.command()
        self.assertEqual(status, 0)
        self.assertIn("SESSION feature\\n\\x1b[2J", out)
        self.assertIn("first\\n\\x1b[2J", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("0.01", out)  # round the sum, not the displayed container values
        self.assertIn("2.00 KiB", out)
        self.assertEqual(out.count("TOTAL"), 1)
        self.assertEqual(err, "")
        ids = [c["id"] for c in self.entries]
        self.assertTrue(all(cid in out for cid in ids))  # fixture IDs collide through the first 63 digits

    def test_container_listing_and_session_listing_share_discovery(self):
        session = self.session()
        self.container(session, "proxy", name="not-a-compose-name")
        self.assertEqual(self.agentbox["_list_sessions"](False)[0]["status"], "stopped")
        self.container(session, name="also-not-a-compose-name")
        self.assertEqual(self.agentbox["_list_sessions"](False)[0]["status"], "running")
        entries = self.agentbox["_list_session_containers"](session)
        self.assertEqual(len(entries), 2)
        self.assertIsInstance(entries[0]["pid"], int)
        self.assertFalse(any("inspect" in c for c in self.calls))


@unittest.skipUnless(os.environ.get("AGENTBOX_TEST_PODMAN") == "1", "Set AGENTBOX_TEST_PODMAN=1 for real Podman tests")
class StatsPodmanTest(unittest.TestCase):
    def setUp(self):
        self.image = os.environ.get("AGENTBOX_TEST_IMAGE", "localhost/agentbox-stats-test:latest")
        subprocess.run(["podman", "image", "exists", self.image], check=True)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.g = self.agentbox["_collect_stats"].__globals__
        p = patch.dict(self.g, AGENTBOX_STATE=self.root / "state")
        p.start()
        self.addCleanup(p.stop)
        self.ids = []
        self.addCleanup(self.remove_containers)

    def remove_containers(self):
        if self.ids:
            subprocess.run(["podman", "rm", "-f", *self.ids], check=True, capture_output=True)

    def session(self, name, persistent=False):
        project = self.root / name
        project.mkdir()
        session = self.g["AGENTBOX_STATE"] / "sessions" / self.agentbox["_session_id"](name, project)
        session.mkdir(parents=True)
        (session / "compose.yaml").write_text(yaml.safe_dump({
            "x-metadata": {"name": name, "project-dir": str(project), "persist": persistent},
        }))
        return session

    def container(self, session, service, name):
        cid = subprocess.run([
            "podman", "run", "-d", "--network", "none", "--name", f"{self.root.name}-{name}",
            "--label", f"com.docker.compose.project={self.agentbox['_project_name'](session)}",
            "--label", f"com.docker.compose.service={service}", self.image,
            "/bin/sh", "-c", "printf '%1048576s' x > /written; while :; do :; done",
        ], check=True, capture_output=True, text=True).stdout.strip()
        self.ids.append(cid)
        return cid

    def test_running_paused_stopped_and_removed_containers(self):
        session = self.session("retained", persistent=True)
        agent = self.container(session, "agent", "agent")
        proxy = self.container(session, "proxy", "proxy")
        snapshot = self.agentbox["_collect_stats"]()
        self.assertTrue(snapshot["complete"], snapshot["errors"])
        group = snapshot["sessions"][0]
        self.assertEqual({c["id"] for c in group["containers"]}, {agent, proxy})
        self.assertGreater(group["totals"]["cpu_percent"], 0)
        self.assertGreater(group["totals"]["memory_bytes"], 0)
        self.assertGreaterEqual(group["totals"]["disk_bytes"], 2 * 1048576)
        env = {**os.environ, "AGENTBOX_STATE": str(self.g["AGENTBOX_STATE"])}
        result = subprocess.run([str(ROOT / "bin" / "agentbox"), "stats", "--session", session.name, "--json"],
                                env=env, cwd=self.root, check=True, capture_output=True, text=True)
        self.assertTrue(json.loads(result.stdout)["complete"])
        self.assertEqual(result.stderr, "")
        result = subprocess.run([str(ROOT / "bin" / "agentbox"), "stats", "--name", "retained"],
                                env=env, cwd=self.root / "retained", check=True, capture_output=True, text=True)
        self.assertIn("DISK (RW)", result.stdout)
        self.assertIn("TOTAL", result.stdout)
        for c in group["containers"]:
            size = subprocess.run(["podman", "container", "inspect", "--size", "--format", "{{.SizeRw}}", c["id"]],
                                  check=True, capture_output=True, text=True)
            self.assertEqual(c["disk_bytes"], int(size.stdout))
        subprocess.run(["podman", "pause", agent], check=True, capture_output=True)
        paused = self.agentbox["_collect_stats"]()
        self.assertTrue(paused["complete"], paused["errors"])
        self.assertEqual(paused["sessions"][0]["containers"][0]["state"], "paused")
        self.assertGreater(paused["sessions"][0]["containers"][0]["memory_bytes"], 0)
        subprocess.run(["podman", "unpause", agent], check=True, capture_output=True)
        subprocess.run(["podman", "stop", "-t", "1", agent, proxy], check=True, capture_output=True)
        stopped = self.agentbox["_collect_stats"]()
        self.assertTrue(stopped["complete"], stopped["errors"])
        totals = stopped["sessions"][0]["totals"]
        self.assertEqual(totals["cpu_percent"], 0)
        self.assertEqual(totals["memory_bytes"], 0)
        self.assertGreaterEqual(totals["disk_bytes"], 2 * 1048576)
        subprocess.run(["podman", "rm", agent], check=True, capture_output=True)
        self.ids.remove(agent)
        self.assertEqual(len(self.agentbox["_collect_stats"]()["sessions"][0]["containers"]), 1)

    def test_multiple_sessions_concurrent_agents_and_unrelated_container(self):
        first = self.session("first")
        second = self.session("second")
        self.container(first, "proxy", "first-proxy")
        self.container(first, "agent", "first-agent-1")
        self.container(first, "agent", "first-agent-2")
        self.container(second, "agent", "second-agent")
        orphan = self.root / "orphan"
        orphan.mkdir()
        (orphan / "compose.yaml").write_text(yaml.safe_dump({"x-metadata": {"name": "orphan", "project-dir": str(self.root)}}))
        self.container(orphan, "agent", "orphan")
        snapshot = self.agentbox["_collect_stats"]()
        self.assertTrue(snapshot["complete"], snapshot["errors"])
        self.assertEqual([len(s["containers"]) for s in snapshot["sessions"]], [3, 1])
        selected = self.agentbox["_collect_stats"]([second])
        self.assertTrue(selected["complete"], selected["errors"])
        self.assertEqual(len(selected["sessions"]), 1)
        self.assertEqual(len(selected["sessions"][0]["containers"]), 1)

    def test_compose_service_labels_and_retained_stop(self):
        session = self.session("composed", persistent=True)
        cfg = yaml.safe_load((session / "compose.yaml").read_text())
        cfg["services"] = {service: {
            "image": self.image, "network_mode": "none",
            "container_name": self.agentbox["_persistent_name"](session, service),
            "command": ["/bin/sh", "-c", "echo retained > /written; while :; do :; done"],
        } for service in ("agent", "proxy")}
        (session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        compose = self.agentbox["compose"]
        self.addCleanup(lambda: compose(session, "down", "-v", check=True, capture_output=True))
        compose(session, "up", "-d", check=True, capture_output=True)
        running = self.agentbox["_collect_stats"]([session])
        self.assertTrue(running["complete"], running["errors"])
        containers = running["sessions"][0]["containers"]
        self.assertEqual({c["service"] for c in containers}, {"agent", "proxy"})
        ids = {c["id"] for c in containers}
        compose(session, "stop", check=True, capture_output=True)
        stopped = self.agentbox["_collect_stats"]([session])
        self.assertTrue(stopped["complete"], stopped["errors"])
        self.assertEqual({c["id"] for c in stopped["sessions"][0]["containers"]}, ids)
        self.assertEqual(stopped["sessions"][0]["totals"]["memory_bytes"], 0)
        compose(session, "start", check=True, capture_output=True)
        restarted = self.agentbox["_collect_stats"]([session])
        self.assertTrue(restarted["complete"], restarted["errors"])
        self.assertEqual({c["id"] for c in restarted["sessions"][0]["containers"]}, ids)


if __name__ == "__main__":
    unittest.main()
