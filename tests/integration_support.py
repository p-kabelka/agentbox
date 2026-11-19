"""Shared session fixture for opt-in, real Podman/Compose integration tests."""

import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("AGENTBOX_INTEGRATION") == "1", "Set AGENTBOX_INTEGRATION=1 for real container tests")
class AgentboxIntegrationTest(unittest.TestCase):
    def setUp(self):
        if not shutil.which("podman"):
            self.fail("Install Podman and podman-compose before running integration tests")
        temporary = tempfile.TemporaryDirectory(prefix="agentbox-integration-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "installation"
        self.home.mkdir()
        for name in ("agent", "proxy", "git", "network", "compose-base.yaml", "presets"):
            (self.home / name).symlink_to(ROOT / name)
        preset = self.home / "custom" / "presets" / "integration"
        preset.mkdir(parents=True)
        (preset / "proxy.yaml").write_text("providers: []\nextra_request_policy: []\n")
        self.use_passt = os.environ.get("AGENTBOX_TEST_USE_PASST") == "1"
        agent_cfg = {"dotfiles": {".gitconfig": "[user]\nname=Agent\nemail=agent@example.com\n"}}
        if self.use_passt:
            agent_cfg["annotations"] = {"krun.use_passt": "1"}
        (preset / "agent.yaml").write_text(yaml.safe_dump(agent_cfg))
        helpers = self.root / "bin"
        helpers.mkdir()
        tag = os.environ.get("AGENTBOX_IMAGE_TAG") or "latest"
        (helpers / f"git-remote-agentbox-{tag}").symlink_to(ROOT / "bin" / "git-remote-agentbox")
        self.env = {**os.environ, "AGENTBOX_HOME": str(self.home), "AGENTBOX_STATE": str(self.root / "state"),
                    "PATH": f"{helpers}:{ROOT / 'bin'}:{Path(sys.executable).parent}:{os.environ['PATH']}"}
        self.session_id = "feature-" + hashlib.sha256(str(self.project.resolve()).encode()).hexdigest()
        self.session = self.root / "state" / "sessions" / self.session_id
        self.remote = f"agentbox-{self.session_id}"
        self.git(self.project, "init", "-q", "--initial-branch=main")
        (self.project / "source").write_text("host\n")
        self.git(self.project, "add", ".")
        self.git(self.project, "commit", "-qm", "initial")
        self.addCleanup(self.cleanup_session)

    def run_command(self, args, *, check=True, **kwargs):
        try:
            result = subprocess.run(args, env=self.env, cwd=self.project, capture_output=True, text=True,
                                    timeout=kwargs.pop("timeout", 120), **kwargs)
        except subprocess.TimeoutExpired as exc:
            def text(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""
            self.fail(f"{args!r} timed out\n{text(exc.stdout)}\n{text(exc.stderr)}")
        if check:
            self.assertEqual(result.returncode, 0, f"{args!r}\n{result.stdout}\n{result.stderr}")
        return result

    def cli(self, *args, **kwargs):
        return self.run_command([sys.executable, str(ROOT / "bin" / "agentbox"), *args], **kwargs)

    def git(self, path, *args, **kwargs):
        return self.run_command(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.com", *args], **kwargs)

    def cleanup_session(self):
        if (self.session / "compose.yaml").is_file():
            self.cli("remove", "--session", self.session_id, check=False)

    def config(self):
        return yaml.safe_load((self.session / "compose.yaml").read_text())

    def repositories(self):
        path = self.session / "git-repositories.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def initialize(self, *args):
        self.cli("init", str(self.project), "--name", "feature", "--preset", "integration", *args)
        cfg = self.config()
        self.assertEqual(cfg["services"]["agent"]["runtime"], "krun")
        # The runtime override is test-local and explicit; production stays krun.
        runtime = os.environ.get("AGENTBOX_TEST_RUNTIME", "krun")
        if runtime != "krun":
            cfg["services"]["agent"]["runtime"] = runtime
            (self.session / "compose.yaml").write_text(yaml.safe_dump(cfg))
        return cfg

    def inspect_git(self):
        return self.inspect_service("git")

    def inspect_service(self, service):
        name = self.config()["services"][service]["container_name"]
        return json.loads(self.run_command(["podman", "inspect", name]).stdout)[0]

    def assert_isolation(self):
        cfg = self.config()
        container = self.inspect_git()
        self.assertEqual(container["Config"]["User"], "git")
        self.assertTrue(container["HostConfig"]["ReadonlyRootfs"])
        self.assertTrue(all(mount["Type"] == "volume" for mount in container["Mounts"]))
        self.assertFalse(container["HostConfig"].get("PortBindings"))
        networks = self.inspect_service("git-network")["NetworkSettings"]["Networks"]
        self.assertEqual(len(networks), 1)
        network = json.loads(self.run_command(["podman", "network", "inspect", next(iter(networks))]).stdout)[0]
        self.assertTrue(network["internal"])
        for service in cfg["services"].values():
            for volume in service.get("volumes", []):
                parts = volume.split(":")
                if parts[0] not in cfg["volumes"]:
                    self.assertIn("ro", parts[2].split(","))

    def read_agent_output(self, process, marker, timeout=15):
        output = bytearray()
        deadline = time.monotonic() + timeout
        while marker not in output.splitlines():
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            output.extend(chunk)
        self.assertIn(marker, output.splitlines(), output.decode(errors="replace"))
