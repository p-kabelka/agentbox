"""Image selection at init and build-time inheritance, without running containers."""

import importlib.util
import io
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ImageTagsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.session = self.root / "session"
        self.session.mkdir()
        env = patch.dict(os.environ, AGENTBOX_HOME=str(ROOT), AGENTBOX_STATE=str(self.root / "state"))
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGENTBOX_IMAGE_TAG", None)
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.g = self.agentbox["main"].__globals__
        self.run_subprocess = subprocess.run

    def native_compose_config(self, args, **kwargs):
        if importlib.util.find_spec("podman_compose") is None:
            self.skipTest("Install podman-compose to verify native image interpolation")
        self.assertEqual(args[:2], ["podman", "compose"])
        self.assertEqual(args[-1], "config")
        # Exercise the real provider's config parser without requiring Podman.
        return self.run_subprocess(
            [sys.executable, "-W", "ignore::DeprecationWarning", "-m", "podman_compose", "--dry-run", *args[2:]],
            cwd=self.project, stderr=subprocess.PIPE, **kwargs,
        )

    def generate(self, agent_cfg, tag):
        with (patch.dict(os.environ),
              patch.object(subprocess, "run", side_effect=self.native_compose_config)):
            if tag is None:
                os.environ.pop("AGENTBOX_IMAGE_TAG", None)
            else:
                os.environ["AGENTBOX_IMAGE_TAG"] = tag
            self.agentbox["generate_compose"](
                self.session, self.project, "testing", 9876, {}, agent_cfg, [])
        return yaml.safe_load((self.session / "compose.yaml").read_text())

    def test_image_tag_environment_defaults(self):
        for value, expected in ((None, "latest"), ("", "latest"), ("stage", "stage")):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is not None:
                    os.environ["AGENTBOX_IMAGE_TAG"] = value
                agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
                self.assertEqual(agentbox["AGENTBOX_IMAGE_TAG"], expected)

    def test_base_and_builtin_presets_inherit_selected_tag(self):
        presets = {None: "base", "default": "base", "claude-vertex": "claude",
                   "cursor": "cursor", "opencode-vertex": "opencode"}
        for tag in ("latest", "stage"):
            for preset, harness in presets.items():
                with self.subTest(tag=tag, preset=preset):
                    agent_cfg = yaml.safe_load((ROOT / "presets" / preset / "agent.yaml").read_text()) if preset else {}
                    services = self.generate(agent_cfg, tag)["services"]
                    self.assertEqual(services["proxy"]["image"], f"localhost/agentbox-proxy:{tag}")
                    self.assertEqual(services["agent"]["image"], f"localhost/agentbox-agent-{harness}:{tag}")
                    self.assertNotIn("${", services["agent"]["image"])
                    self.assertNotIn("${", services["proxy"]["image"])

    def test_custom_image_override_can_inherit_tag_without_expanding_environment(self):
        for placeholder in ("${AGENTBOX_IMAGE_TAG:-latest}", "${AGENTBOX_IMAGE_TAG}",
                            "$AGENTBOX_IMAGE_TAG", "${AGENTBOX_IMAGE_TAG-stable}",
                            "${AGENTBOX_IMAGE_TAG:?choose a tag}"):
            with self.subTest(placeholder=placeholder):
                cfg = {"agent_image": f"registry.example:5000/team/custom:{placeholder}",
                       "environment": {"CUSTOM": "${CUSTOM:-unchanged}"},
                       "annotations": {"krun.ram_mib": "4096"}}
                agent = self.generate(cfg, "stage")["services"]["agent"]
                self.assertEqual(agent["image"], "registry.example:5000/team/custom:stage")
                self.assertEqual(agent["environment"]["CUSTOM"], "${CUSTOM:-unchanged}")
                self.assertEqual(agent["annotations"], cfg["annotations"])
                self.assertEqual(cfg["agent_image"], f"registry.example:5000/team/custom:{placeholder}")

    def test_native_compose_defaults_for_unset_and_empty_values(self):
        for tag in (None, ""):
            with self.subTest(tag=tag):
                cfg = {"agent_image": "custom:${AGENTBOX_IMAGE_TAG:-fallback}"}
                services = self.generate(cfg, tag)["services"]
                self.assertEqual(services["proxy"]["image"], "localhost/agentbox-proxy:latest")
                self.assertEqual(services["agent"]["image"], "custom:fallback")

    def test_native_compose_expands_other_image_variables(self):
        with patch.dict(os.environ, AGENTBOX_TEST_REGISTRY="registry.example:5000", AGENTBOX_TEST_IMAGE="team/custom"):
            cfg = {"agent_image": "$AGENTBOX_TEST_REGISTRY/${AGENTBOX_TEST_IMAGE}:${AGENTBOX_IMAGE_TAG}"}
            services = self.generate(cfg, "stage")["services"]
        self.assertEqual(services["agent"]["image"], "registry.example:5000/team/custom:stage")

    def test_image_interpolation_does_not_load_local_dotenv(self):
        (self.project / ".env").write_text("AGENTBOX_IMAGE_TAG=unexpected\n")
        services = self.generate({}, None)["services"]
        self.assertEqual(services["proxy"]["image"], "localhost/agentbox-proxy:latest")
        self.assertEqual(services["agent"]["image"], "localhost/agentbox-agent-base:latest")

    def test_required_image_variable_failure_preserves_saved_compose(self):
        self.generate({}, "stage")
        original = (self.session / "compose.yaml").read_bytes()
        with self.assertRaises(subprocess.CalledProcessError) as result:
            self.generate({"agent_image": "custom:${AGENTBOX_IMAGE_TAG:?choose a tag}"}, "")
        self.assertIn("choose a tag", result.exception.stderr)
        self.assertEqual((self.session / "compose.yaml").read_bytes(), original)

    def test_compose_config_receives_only_image_fields(self):
        resolved = {"services": {"proxy": {"image": "localhost/agentbox-proxy:stage"},
                                 "agent": {"image": "custom:stage"}}}
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=yaml.safe_dump(resolved))) as run:
            self.agentbox["generate_compose"](
                self.session, self.project, "testing", 9876,
                {"environment": {"API_KEY": "${API_KEY}"}},
                {"agent_image": "custom:${AGENTBOX_IMAGE_TAG:-latest}"}, [])
        args = run.call_args.args[0]
        self.assertEqual(args, ["podman", "compose", "--env-file", os.devnull,
                                "--project-name", "agentbox-images", "-f", "-", "config"])
        sent = yaml.safe_load(run.call_args.kwargs["input"])
        self.assertEqual(set(sent), {"services"})
        for service in sent["services"].values():
            self.assertEqual(set(service), {"image"})
        self.assertEqual(sent["services"]["agent"]["image"], "custom:${AGENTBOX_IMAGE_TAG:-latest}")
        self.assertTrue(run.call_args.kwargs["check"])
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        self.assertEqual(cfg["services"]["proxy"]["environment"]["API_KEY"], "${API_KEY}")
        self.assertEqual(cfg["services"]["agent"]["image"], "custom:stage")

    def test_pinned_custom_image_overrides_are_unchanged(self):
        for image in ("registry.example:5000/team/custom:latest", "custom:v1.2.3", "custom",
                      "registry.example/team/custom@sha256:" + "a" * 64):
            with self.subTest(image=image):
                services = self.generate({"agent_image": image}, "stage")["services"]
                self.assertEqual(services["agent"]["image"], image)
                self.assertEqual(services["proxy"]["image"], "localhost/agentbox-proxy:stage")

    def test_init_freezes_images_across_start_from_a_different_environment(self):
        init_args = ["agentbox", "init", str(self.project), "--name", "testing",
                     "--preset", "opencode-vertex", "--no-git"]
        with (patch.dict(self.g, free_port=lambda: 9876),
              patch.dict(os.environ, AGENTBOX_IMAGE_TAG="stage"),
              patch.object(subprocess, "run", side_effect=self.native_compose_config),
              patch.object(sys, "argv", init_args), redirect_stdout(io.StringIO())):
            self.agentbox["main"]()
        session_id = self.agentbox["_session_id"]("testing", self.project)
        session = self.root / "state" / "sessions" / session_id
        original = (session / "compose.yaml").read_bytes()
        services = yaml.safe_load(original)["services"]
        self.assertEqual(services["proxy"]["image"], "localhost/agentbox-proxy:stage")
        self.assertEqual(services["agent"]["image"], "localhost/agentbox-agent-opencode:stage")

        # A new invocation defaults to latest, but start must use the saved images.
        agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.assertEqual(agentbox["AGENTBOX_IMAGE_TAG"], "latest")
        with (patch.dict(agentbox["main"].__globals__,
                         _synchronize_proxy=lambda *args: None),
              patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
              patch.object(sys, "argv", ["agentbox", "start", "--session", session_id]),
              self.assertRaises(SystemExit) as result):
            agentbox["main"]()
        self.assertEqual(result.exception.code, 0)
        self.assertEqual((session / "compose.yaml").read_bytes(), original)
        self.assertEqual(run.call_args_list[0].args[0][-3:], ["up", "-d", "proxy"])
        self.assertEqual(run.call_args_list[1].args[0][-4:], ["run", "--rm", "--no-deps", "agent"])

    def test_build_update_defaults_and_explicit_tag_override(self):
        harnesses = ["base", "proxy", "opencode", "claude", "codex", "cursor"]
        for command in ("build", "update"):
            for env_tag in ("latest", "stage"):
                for override in (None, "canary", "latest"):
                    with self.subTest(command=command, env_tag=env_tag, override=override):
                        argv = ["agentbox", command, *harnesses]
                        if override is not None:
                            argv += ["--tag", override]
                        tag = override or env_tag
                        with (patch.dict(self.g, AGENTBOX_IMAGE_TAG=env_tag),
                              patch.dict(os.environ, AGENTBOX_BUILD_OPENCODE_SKIP_VERIFICATION="1"),
                              patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()),
                              patch.object(subprocess, "run") as run):
                            self.agentbox["main"]()
                        self.assertEqual(run.call_count, len(harnesses))
                        for harness, call in zip(harnesses, run.call_args_list):
                            args = call.args[0]
                            ctx = ROOT / ("proxy" if harness == "proxy" else "agent")
                            image = "agentbox-proxy" if harness == "proxy" else f"agentbox-agent-{harness}"
                            self.assertEqual(args[:2], ["podman", "build"])
                            self.assertEqual(args[args.index("-t") + 1], f"localhost/{image}:{tag}")
                            self.assertEqual(args[args.index("-f") + 1],
                                             str(ctx / self.g["HARNESS_CONTAINERFILES"][harness]))
                            self.assertEqual(args[-1], str(ctx))
                            self.assertEqual("--no-cache" in args, command == "update")
                            if harness not in ("base", "proxy"):
                                self.assertEqual(args[args.index("--build-arg") + 1], f"AGENTBOX_IMAGE_TAG={tag}")
                            self.assertEqual("AGENTBOX_BUILD_OPENCODE_SKIP_VERIFICATION=1" in args,
                                             harness == "opencode")
                            self.assertTrue(call.kwargs["check"])

    def test_build_helper_uses_configured_tag_by_default(self):
        with (patch.dict(self.g, AGENTBOX_IMAGE_TAG="stage"), redirect_stdout(io.StringIO()),
              patch.object(subprocess, "run") as run):
            self.agentbox["_build_images"](harnesses=["codex"])
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-t") + 1], "localhost/agentbox-agent-codex:stage")
        self.assertEqual(args[args.index("--build-arg") + 1], "AGENTBOX_IMAGE_TAG=stage")

    def test_default_build_includes_base_before_harnesses(self):
        with (patch.dict(self.g, AGENTBOX_IMAGE_TAG="stage"),
              patch.dict(os.environ, AGENTBOX_BUILD_OPENCODE_SKIP_VERIFICATION="1"),
              patch.object(sys, "argv", ["agentbox", "build"]), redirect_stdout(io.StringIO()),
              patch.object(subprocess, "run") as run):
            self.agentbox["main"]()
        images = [call.args[0][call.args[0].index("-t") + 1] for call in run.call_args_list]
        expected = {f"localhost/agentbox-agent-{name}:stage"
                    for name in ("base", "claude", "codex", "cursor", "opencode")}
        expected.add("localhost/agentbox-proxy:stage")
        self.assertEqual(images[0], "localhost/agentbox-agent-base:stage")
        self.assertEqual(set(images), expected)

    def test_tagged_build_preserves_verification_secret(self):
        with (patch.dict(self.g, _resolve_gh_token=lambda: "test-token"),
              patch.dict(os.environ, AGENTBOX_BUILD_OPENCODE_SKIP_VERIFICATION=""),
              redirect_stdout(io.StringIO()), patch.object(subprocess, "run") as run):
            self.agentbox["_build_images"](harnesses=["opencode"], tag="stage")
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--build-arg") + 1], "AGENTBOX_IMAGE_TAG=stage")
        self.assertEqual(args[args.index("--secret") + 1], "id=gh_token,env=_AGENTBOX_GH_TOKEN")
        self.assertEqual(run.call_args.kwargs["env"]["_AGENTBOX_GH_TOKEN"], "test-token")
        self.assertNotIn("test-token", args)

    def test_harness_containerfiles_use_selected_base_tag(self):
        for path in sorted((ROOT / "agent").glob("Containerfile.*")):
            if path.name == "Containerfile.base":
                continue
            with self.subTest(containerfile=path.name):
                lines = path.read_text().splitlines()
                arg = lines.index("ARG AGENTBOX_IMAGE_TAG=latest")
                first_from = next(i for i, line in enumerate(lines) if line.startswith("FROM "))
                self.assertLess(arg, first_from)
                self.assertIn("FROM localhost/agentbox-agent-base:${AGENTBOX_IMAGE_TAG}", lines)


if __name__ == "__main__":
    unittest.main()
