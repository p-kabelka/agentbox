"""Zsh completion contract and real TAB completion, without running containers."""

import argparse
import hashlib
import json
import os
import pty
import re
import runpy
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPLETION = ROOT / "completions" / "zsh" / "_agentbox"
ZSH = shutil.which("zsh")


@unittest.skipUnless(ZSH, "Install zsh to test shell completions")
class CompletionContractTest(unittest.TestCase):
    def test_every_command_option_alias_and_positional_has_a_definition(self):
        agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))

        def check(parser, path=()):
            # Capture the specs passed to real zsh's _arguments. The live TAB
            # tests below separately exercise their actual completion behavior.
            script = """
_arguments() { print -rl -- "$@"; state=subcommand; }
_describe() { print -rl -- "${subcommands[@]}"; }
words=(agentbox "$@" '')
CURRENT=$#words
source "$COMPLETION"
"""
            result = subprocess.run(
                [ZSH, "-fc", script, "zsh", *path], capture_output=True, text=True,
                env={**os.environ, "COMPLETION": str(COMPLETION)}, check=True,
            )
            self.assertEqual(result.stderr, "", path)
            specs = result.stdout.splitlines()
            options = set()
            takes_value = {}
            for spec in specs:
                if "[" in spec:
                    head = spec.split("[", 1)[0].rsplit(")", 1)[-1]
                    flags = re.findall(r"--?[a-z][a-z-]*", head)
                    options.update(flags)
                    takes_value.update({flag: spec.split("]", 1)[1].startswith(":") for flag in flags})
            expected_options = {flag for action in parser._actions for flag in action.option_strings}
            self.assertEqual(options, expected_options, path)
            for action in parser._actions:
                for flag in action.option_strings:
                    self.assertEqual(takes_value[flag], action.nargs != 0, (path, flag))

            positionals = [a for a in parser._actions if not a.option_strings]
            positional_specs = [s for s in specs if re.match(r"^(\d+|\*):", s)]
            self.assertEqual(len(positional_specs), len(positionals), path)
            for action, spec in zip(positionals, positional_specs):
                if isinstance(action, argparse._SubParsersAction):
                    commands = {s.split(":", 1)[0] for s in specs if re.match(r"^[a-z][a-z-]*:", s)}
                    self.assertEqual(commands, set(action.choices), path)
                    for name, child in action.choices.items():
                        check(child, (*path, name))
                else:
                    # Even free-form arguments must have an explicit spec, so
                    # zsh does not accidentally fall back to local filenames.
                    if action.nargs in ("*", "+"):
                        self.assertTrue(spec.startswith("*:"), (path, action.dest))
                    for choice in action.choices or ():
                        self.assertIn(choice, spec, (path, action.dest))

        check(agentbox["build_parser"]())

    def test_zsh_syntax(self):
        subprocess.run([ZSH, "-n", str(COMPLETION)], check=True)


@unittest.skipUnless(ZSH, "Install zsh to test shell completions")
class LiveCompletionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="agentbox completions ")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        self.home = self.root / "installation"
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "agentbox").symlink_to(ROOT / "bin" / "agentbox")
        (self.home / "agent").mkdir(parents=True)
        for harness in ("base", "claude", "custom-harness"):
            (self.home / "agent" / f"Containerfile.{harness}").touch()
        for name in ("default", "personal setup"):
            (self.home / "presets" / name).mkdir(parents=True)
        (self.home / "custom" / "presets" / "custom").mkdir(parents=True)
        self.podman_calls = self.root / "podman.jsonl"
        podman = self.bin / "podman"
        podman.write_text(f"""#!{sys.executable}
import json, sys
with open({str(self.podman_calls)!r}, 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'images':
    print('stage\\nlatest\\n<none>')
elif sys.argv[1] != 'ps':
    sys.exit(99)
""")
        podman.chmod(0o755)
        self.env = {
            **os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
            "AGENTBOX_HOME": str(self.home), "AGENTBOX_STATE": str(self.state),
            "AGENTBOX_IMAGE_TAG": "testing", "TERM": "xterm", "ZDOTDIR": str(self.root),
        }
        self.session_id = self.session("feature", self.project)
        self.foreign_id = self.session("other", self.root / "elsewhere")
        self.config_path = self.state / "sessions" / self.session_id / "proxy-config" / "proxy.yaml"
        self.config_path.parent.mkdir()
        self.config_path.write_text(yaml.safe_dump({
            "providers": [{"name": "openai"}, {"name": "anthropic", "enabled": True},
                          {"name": "space name"}, {"name": "colon:name"}],
            "extra_request_policy": [{"host": r"example\.com"},
                                     {"host": r"internal\.test", "port": 8443},
                                     {"host": r".*\.example\.org"}],
        }))
        foreign_config = self.state / "sessions" / self.foreign_id / "proxy-config" / "proxy.yaml"
        foreign_config.parent.mkdir()
        foreign_config.write_text(yaml.safe_dump({"providers": [{"name": "foreign-provider"}]}))
        self.matches_file = self.root / "matches"
        setup = self.root / "setup.zsh"
        setup.write_text(f"""
bindkey -e
fpath=({shlex.quote(str(COMPLETION.parent))} $fpath)
autoload -Uz compinit
compinit -D -i
unsetopt beep
LISTMAX=10000
zstyle ':completion:*' menu no
compadd() {{
  local -a captured
  builtin compadd -A captured "$@"
  if (( $#captured )); then
    captured=("${{(@Q)captured}}")
    print -rn -- "${{(pj:\\0:)captured}}"$'\\0' >> {shlex.quote(str(self.matches_file))}
  fi
  builtin compadd "$@"
}}
_test_complete() {{
  _main_complete
  print -r -- '__COMPLETION_DONE__'
}}
zle -C test-complete complete-word _test_complete
bindkey '^I' test-complete
print -r -- '__COMPLETION_READY__'
""")
        self.master, slave = pty.openpty()
        self.shell = subprocess.Popen(
            [ZSH, "-dfi"], stdin=slave, stdout=slave, stderr=slave,
            env=self.env, cwd=self.project,
        )
        os.close(slave)
        self.addCleanup(self.close_shell)
        os.write(self.master, f"source {shlex.quote(str(setup))}\n".encode())
        self.read_until(b"__COMPLETION_READY__")

    def close_shell(self):
        self.shell.kill()
        self.shell.wait()
        os.close(self.master)

    def session(self, name, project):
        sid = f"{name}-{hashlib.sha256(str(project.resolve()).encode()).hexdigest()}"
        session = self.state / "sessions" / sid
        session.mkdir(parents=True)
        (session / "compose.yaml").write_text(yaml.safe_dump({
            "x-metadata": {"name": name, "project-dir": str(project)},
        }))
        return sid

    def read_until(self, marker):
        output = b""
        deadline = time.monotonic() + 15
        while marker not in output:
            ready, _, _ = select.select([self.master], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                self.fail(f"zsh did not finish completion: {output.decode(errors='replace')}")
            output += os.read(self.master, 65536)
        return output.decode(errors="replace")

    def complete(self, before, after=""):
        self.matches_file.write_bytes(b"")
        os.write(self.master, b"\x15" + (before + after).encode() + b"\x02" * len(after) + b"\t")
        output = self.read_until(b"__COMPLETION_DONE__")
        self.assertNotIn("command not found", output)
        self.assertNotIn("bad ", output)
        return set(filter(None, self.matches_file.read_text().split("\0")))

    def test_commands_and_nested_commands(self):
        self.assertIn("init", self.complete("agentbox "))
        self.assertEqual(self.complete("agentbox proxy "), {"reload", "restart", "provider"})
        self.assertEqual(self.complete("agentbox proxy provider "), {"list", "enable", "disable"})
        self.assertEqual(self.complete("agentbox preset "), {"list", "edit", "copy"})

    def test_option_completion_for_every_command_and_alias(self):
        agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))

        def check(parser, path=()):
            line = " ".join(("agentbox", *path, "-"))
            expected = {flag for action in parser._actions for flag in action.option_strings}
            self.assertEqual(self.complete(line), expected, path)
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for name, child in action.choices.items():
                        check(child, (*path, name))

        check(agentbox["build_parser"]())
        self.assertFalse({"--name", "-n", "--session", "-s"} & self.complete("agentbox logs -n feature -"))
        self.assertIn("--ro-mount", self.complete("agentbox init --ro-mount /tmp --r"))

    def test_session_ids_and_names_with_all_option_spellings(self):
        for flag in ("--session ", "--session=", "-s ", "-s"):
            with self.subTest(flag=flag):
                self.assertEqual(self.complete("agentbox stop " + flag), {self.session_id, self.foreign_id})
        for flag in ("--name ", "--name=", "-n ", "-n"):
            with self.subTest(flag=flag):
                self.assertEqual(self.complete("agentbox logs " + flag), {"feature"})
        self.assertEqual(self.complete("agentbox proxy restart --session "), {self.session_id, self.foreign_id})
        new_id = self.session("new", self.project)
        self.assertIn(new_id, self.complete("agentbox stop --session "))

    def test_presets_and_harnesses_are_live(self):
        presets = {"default", "custom", "personal setup"}
        self.assertEqual(self.complete("agentbox init --preset "), presets)
        self.assertEqual(self.complete("agentbox preset edit proxy "), presets)
        self.assertEqual(self.complete("agentbox preset copy "), presets)
        self.assertEqual(self.complete("agentbox preset edit "), {"proxy", "agent"})
        self.assertEqual(self.complete("agentbox build "), {"base", "claude", "custom-harness", "proxy"})
        self.assertNotIn("base", self.complete("agentbox update base "))
        self.assertIn("base", self.complete("agentbox build --tag base "))
        (self.home / "agent" / "Containerfile.added").touch()
        self.assertIn("added", self.complete("agentbox build "))

    def test_providers_follow_selectors_before_and_after_cursor(self):
        providers = {"openai", "anthropic", "space name", "colon:name"}
        self.assertEqual(self.complete("agentbox proxy provider enable "), providers)
        self.assertEqual(self.complete("agentbox proxy provider disable openai "), providers - {"openai"})
        self.assertEqual(self.complete("agentbox proxy provider enable 'space name' "), providers - {"space name"})
        for selector in (f"--session {self.foreign_id}", f"--session={self.foreign_id}",
                         f"-s{self.foreign_id}"):
            with self.subTest(selector=selector):
                self.assertEqual(self.complete(f"agentbox proxy provider enable {selector} "), {"foreign-provider"})
                self.assertEqual(self.complete("agentbox proxy provider enable ", " " + selector), {"foreign-provider"})
        for selector in ("--name feature", "--name=feature", "-nfeature"):
            self.assertEqual(self.complete(f"agentbox proxy provider enable {selector} "), providers)
        cfg = yaml.safe_load(self.config_path.read_text())
        cfg["providers"].append({"name": "feature"})
        self.config_path.write_text(yaml.safe_dump(cfg))
        self.assertIn("feature", self.complete("agentbox proxy provider enable --name feature "))

    def test_saved_host_rules_and_regexes(self):
        literal = self.complete("agentbox deny ")
        self.assertTrue({"example.com", "internal.test:8443"} <= literal)
        self.assertNotIn(r".*\.example\.org", literal)
        regexes = self.complete("agentbox deny -r ")
        self.assertEqual(regexes, {r"example\.com", r"internal\.test:8443", r".*\.example\.org"})
        self.assertEqual(self.complete("agentbox deny -rnfeature "), regexes)
        self.assertIn("example.com", self.complete("agentbox allow --name=feature "))

    def test_paths_git_references_and_tags(self):
        (self.project / "reference directory").mkdir()
        (self.project / "source.txt").touch()
        self.assertIn("reference directory", self.complete("agentbox init "))
        self.assertNotIn("source.txt", self.complete("agentbox init "))
        self.assertIn("source.txt", self.complete("agentbox init --ro-mount "))
        self.assertEqual(self.complete("agentbox init --ro-mount reference:destination"), set())
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "commit", "--allow-empty", "-qm", "initial"], check=True)
        subprocess.run(["git", "-C", str(self.project), "branch", "completion-branch"], check=True)
        subprocess.run(["git", "-C", str(self.project), "tag", "completion-tag"], check=True)
        self.assertTrue({"HEAD", "completion-branch", "completion-tag"} <= self.complete("agentbox init --branch "))
        self.assertIn("completion-branch", self.complete(f"agentbox init {shlex.quote(str(self.project))} --branch "))
        self.assertIn("completion-branch", self.complete("agentbox init --branch ", " " + shlex.quote(str(self.project))))
        self.assertEqual(self.complete("agentbox build --tag "), {"latest", "stage", "testing"})

    def test_free_form_values_and_start_command_forwarding(self):
        for line in ("agentbox init --name ", "agentbox logs --tail ", "agentbox preset copy default "):
            self.assertEqual(self.complete(line), set(), line)
        self.assertIn("python3", self.complete("agentbox start -- pyth"))
        self.assertIn("-c", self.complete("agentbox start -- python3 -"))
        self.assertNotIn("--session", self.complete("agentbox start -- python3 -"))

    def test_queries_do_not_mutate_configuration_or_containers(self):
        before = self.config_path.read_bytes()
        for line in ("agentbox stop --session ", "agentbox proxy provider enable ", "agentbox deny ", "agentbox build --tag "):
            self.complete(line)
        self.assertEqual(self.config_path.read_bytes(), before)
        calls = [json.loads(line) for line in self.podman_calls.read_text().splitlines()]
        self.assertTrue(calls)
        self.assertTrue(all(call[0] in ("ps", "images") for call in calls), calls)

    def test_failed_and_malformed_live_queries_are_quiet(self):
        executable = self.bin / "agentbox"
        executable.unlink()
        for body in ("echo invalid-json", "echo query-failed >&2; exit 1", "echo '[]'"):
            executable.write_text("#!/bin/sh\n" + body + "\n")
            executable.chmod(0o755)
            self.assertEqual(self.complete("agentbox stop --session "), set())


if __name__ == "__main__":
    unittest.main()
