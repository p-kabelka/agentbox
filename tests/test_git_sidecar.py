"""Real Git round trips with only the Podman boundary substituted."""

import fcntl
import importlib.util
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server = load_module("git_server", ROOT / "git" / "server.py")
clone = load_module("clone_repos", ROOT / "agent" / "clone_repos.py")


class GitSidecarTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agentbox git ",
                                                dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = self.root / "store"
        self.store.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.git(self.project, "init", "-q", "--initial-branch=main")
        (self.project / "source").write_text("host\n")
        self.git(self.project, "add", ".")
        self.git(self.project, "commit", "-qm", "initial")
        self.session = self.root / "state" / "sessions" / "feature-test"
        self.session.mkdir(parents=True)
        self.remote = f"agentbox-{self.session.name}"
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        podman = self.fake_bin / "podman"
        podman.write_text(f"""#!{sys.executable}
import os, sys
if sys.argv[1] == 'compose':
    sys.exit(0)
assert sys.argv[1:3] == ['exec', '-i'], sys.argv
args = sys.argv[3:]
if args[:2] == ['--env', 'GIT_PROTOCOL']:
    args = args[2:]
args = args[1:]
args[-1] = args[-1].replace('/srv/git', {str(self.store)!r})
os.execvp(args[0], args)
""")
        podman.chmod(0o755)
        environment = patch.dict(os.environ, AGENTBOX_IMAGE_TAG="",
                                 PATH=f"{self.fake_bin}:{ROOT / 'bin'}:{os.environ['PATH']}")
        environment.start()
        self.addCleanup(environment.stop)
        self.agentbox = runpy.run_path(str(ROOT / "bin" / "agentbox"))
        self.g = self.agentbox["main"].__globals__
        self.events = []
        for p in (patch.object(server, "STORE", self.store),
                  patch.dict(self.g, AGENTBOX_HOME=ROOT, AGENTBOX_STATE=self.root / "state",
                             compose=self.compose, _resolve_images=lambda images: images),
                  patch.object(clone, "WORKSPACE", self.root / "sandbox" / "workspace"),
                  patch.object(clone, "CONTEXT", self.root / "sandbox" / "context" / "git"),
                  patch.object(clone, "GIT_URL", (self.store / "repos").as_uri() + "/"),
                  patch.object(clone, "shared_repositories", side_effect=server.published_repositories)):
            p.start()
            self.addCleanup(p.stop)
        self.agentbox["generate_compose"](self.session, self.project, "feature", 9876, {}, {}, [])

    def git(self, path, *args, check=True):
        return subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
                              check=check, capture_output=True, text=True)

    def compose(self, session, *args, **kwargs):
        self.events.append(args)
        if args[:6] == ("exec", "-T", "git", "python3", "/app/server.py", "init"):
            server.initialize(*args[6:])
        elif args[:6] == ("exec", "-T", "git", "python3", "/app/server.py", "publish"):
            server.publish(*args[6:])
        elif args[:6] == ("exec", "-T", "git", "python3", "/app/server.py", "remove"):
            server.remove_repository(*args[6:])
        return subprocess.CompletedProcess(args, 0)

    def share(self, name="workspace", path=None, branch=""):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.agentbox["_mount_repository"](self.session, path or self.project, name, branch)

    def unmount(self, name):
        args = self.agentbox["build_parser"]().parse_args(["unmount", name, "--session", self.session.name])
        with redirect_stdout(io.StringIO()):
            self.agentbox["cmd_unmount"](args)

    def test_concurrent_mounts_preserve_registrations_and_upload_in_parallel(self):
        other = self.root / "other"
        self.git(self.root, "clone", "-q", str(self.project), str(other))
        uploading = threading.Barrier(2)
        run = subprocess.run

        def concurrent_push(args, **kwargs):
            if args[0] == "git" and "push" in args:
                # Both uploads must reach here without holding the metadata lock.
                uploading.wait(timeout=10)
            return run(args, **kwargs)

        with (patch.object(subprocess, "run", side_effect=concurrent_push),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
              ThreadPoolExecutor(2) as pool):
            futures = [pool.submit(self.agentbox["_mount_repository"], self.session, path, name)
                       for path, name in ((self.project, "one"), (other, "two"))]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual(set(self.agentbox["_git_repositories"](self.session)), {"one", "two"})
        self.assertEqual(server.published_repositories(), ["one", "two"])

    def test_catalog_publication_uses_independent_markers(self):
        for name in ("org/one", "org/two"):
            server.initialize(name, "main", "sha1")
            self.git(self.project, "push", str(server.repository(name)), "HEAD:main")
        self.assertEqual(server.published_repositories(), [])
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(server.publish, name, "main") for name in ("org/one", "org/two")]
            for future in futures:
                future.result(timeout=10)
        self.assertEqual(server.published_repositories(), ["org/one", "org/two"])
        self.assertFalse((self.store / "manifest.json").exists())
        server.remove_repository("org/one")
        self.assertEqual(server.published_repositories(), ["org/two"])
        self.assertFalse(server.repository("org/one").exists())

    def test_unmount_deletes_only_selected_sidecar_repo_and_matching_host_remote(self):
        self.share()
        clone.clone_repositories()
        (clone.WORKSPACE / "source").write_text("keep sandbox edits\n")
        other = self.root / "other"
        self.git(self.root, "clone", "-q", str(self.project), str(other))
        self.share("org/library", other)
        worktree = self.root / "worktree"
        self.git(self.project, "worktree", "add", "-qb", "linked", str(worktree))
        compose = (self.session / "compose.yaml").read_bytes()
        self.unmount("workspace")
        self.assertFalse(server.repository("workspace").exists())
        self.assertEqual(server.published_repositories(), ["org/library"])
        self.assertEqual(set(self.agentbox["_git_repositories"](self.session)), {"org/library"})
        self.assertNotIn(self.remote, self.git(worktree, "remote").stdout.splitlines())
        self.assertIn(self.remote, self.git(other, "remote").stdout.splitlines())
        self.assertEqual((self.project / "source").read_text(), "host\n")
        self.assertEqual((clone.WORKSPACE / "source").read_text(), "keep sandbox edits\n")
        self.assertEqual((self.session / "compose.yaml").read_bytes(), compose)
        # A name is reusable after unmounting, including from a different host repo.
        self.unmount("org/library")
        self.share("org/library", self.project)

    def test_unmount_preserves_repointed_remotes_and_cleans_missing_sidecar_repositories(self):
        self.share("org/library")
        self.git(self.project, "remote", "set-url", self.remote, "https://example.invalid/repointed")
        server.remove_repository("org/library")
        self.unmount("org/library")
        self.assertEqual(self.agentbox["_git_repositories"](self.session), {})
        self.assertEqual(self.git(self.project, "remote", "get-url", self.remote).stdout.strip(),
                         "https://example.invalid/repointed")

    def test_unmount_rejects_unknown_or_invalid_names_without_side_effects(self):
        self.share()
        self.events.clear()
        for name in ("missing", "../escape"):
            with self.subTest(name=name), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.unmount(name)
        self.assertEqual(self.events, [])
        self.assertEqual(server.published_repositories(), ["workspace"])

    def test_unmount_can_interrupt_upload_without_republishing_it(self):
        uploaded, release = threading.Event(), threading.Event()
        run = subprocess.run

        def paused_push(args, **kwargs):
            result = run(args, **kwargs)
            if args[0] == "git" and "push" in args:
                uploaded.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release upload")
            return result

        with (patch.object(subprocess, "run", side_effect=paused_push),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
              ThreadPoolExecutor(2) as pool):
            mounting = pool.submit(self.agentbox["_mount_repository"], self.session, self.project, "library")
            try:
                self.assertTrue(uploaded.wait(5))
                pool.submit(self.unmount, "library").result(timeout=5)
            finally:
                release.set()
            with self.assertRaises(SystemExit):
                mounting.result(timeout=5)
        self.assertFalse(server.repository("library").exists())
        self.assertEqual(server.published_repositories(), [])
        self.assertEqual(self.agentbox["_git_repositories"](self.session), {})

    def assert_mount_allows_session_command(self, command):
        pushing, release, finished = threading.Event(), threading.Event(), threading.Event()
        mount_errors, command_errors = [], []
        run = subprocess.run
        args = types.SimpleNamespace(repo=str(self.project), repo_name="library", branch="", name=None,
                                     session=self.session.name)

        def paused_push(arguments, **kwargs):
            if arguments[0] == "git" and "push" in arguments:
                pushing.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release the push")
                raise subprocess.CalledProcessError(143, arguments)
            return run(arguments, **kwargs)

        def mount():
            try:
                self.agentbox["cmd_mount"](args)
            except BaseException as exc:
                mount_errors.append(exc)

        def control():
            try:
                self.agentbox[command](args)
            except BaseException as exc:
                command_errors.append(exc)
            finally:
                finished.set()

        synchronize = Mock()
        with (patch.object(subprocess, "run", side_effect=paused_push),
              patch.dict(self.g, _secret_preflight=lambda session: ("config", "providers", []),
                         _synchronize_proxy=synchronize),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            worker = threading.Thread(target=mount)
            controller = None
            worker.start()
            try:
                self.assertTrue(pushing.wait(5), mount_errors)
                controller = threading.Thread(target=control)
                controller.start()
                self.assertTrue(finished.wait(3), f"{command} waited for the Git push")
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                worker.join(5)
                if controller is not None:
                    controller.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(controller)
        self.assertFalse(controller.is_alive())
        self.assertEqual(command_errors, [])
        self.assertEqual(len(mount_errors), 1)
        self.assertIsInstance(mount_errors[0], subprocess.CalledProcessError)
        self.assertEqual(mount_errors[0].returncode, 143)
        self.assertEqual([event for event in self.events if event[0] == "up"],
                         [("up", "-d", "--no-deps", "--no-recreate", "git")])
        self.assertFalse(any(event[:6] == ("exec", "-T", "git", "python3", "/app/server.py", "publish")
                             for event in self.events))
        if command == "cmd_proxy_reload":
            synchronize.assert_called_once()
        elif command == "cmd_stop":
            self.assertIn(("down",), self.events)
        else:
            self.assertIn(("down", "-v"), self.events)
            self.assertFalse(self.session.exists())

    def test_stop_does_not_wait_for_mount_push(self):
        self.assert_mount_allows_session_command("cmd_stop")

    def test_remove_does_not_wait_for_mount_push(self):
        self.assert_mount_allows_session_command("cmd_remove")

    def test_proxy_reload_does_not_wait_for_mount_push(self):
        self.assert_mount_allows_session_command("cmd_proxy_reload")

    def test_initial_share_runs_after_releasing_secret_sync_lock(self):
        publications = []

        def publish(session, path, name, branch):
            with (session / ".secret-sync.lock").open("a") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            publications.append((path, name, branch))

        args = types.SimpleNamespace(dir=str(self.project), name="initial", persist=False, preset="default",
                                     no_git=False, branch="main", ro_mounts=[], rw_mounts=[], start=False)
        with patch.dict(self.g, _mount_repository=publish), redirect_stdout(io.StringIO()):
            self.agentbox["cmd_init"](args)
        self.assertEqual(publications, [(self.project, "workspace", "main")])

    def test_default_compose_has_no_writable_host_bind_mount_or_published_git_port(self):
        (self.session / "dotfiles").mkdir()
        (self.session / "dotfiles" / ".gitconfig").write_text("[user]\nname=Agent\n")
        self.agentbox["generate_compose"](self.session, self.project, "feature", 9876, {}, {}, [], persist=True)
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        for service in cfg["services"].values():
            for volume in service.get("volumes", []):
                parts = volume.split(":")
                if parts[0] not in cfg["volumes"]:
                    self.assertIn("ro", parts[2].split(","))
        self.assertEqual(cfg["services"]["git"]["networks"], ["agent-net"])
        self.assertNotIn("ports", cfg["services"]["git"])
        self.assertEqual(cfg["services"]["agent"]["runtime"], "krun")
        self.assertIn("./dotfiles:/agentbox-dotfiles:ro,Z", cfg["services"]["agent"]["volumes"])
        self.assertIn("./proxy-config:/config:ro,Z", cfg["services"]["proxy"]["volumes"])

    def test_no_git_is_an_explicit_writable_workspace_mount(self):
        self.agentbox["generate_compose"](self.session, self.project, "feature", 9876, {}, {}, [], no_git=True)
        cfg = yaml.safe_load((self.session / "compose.yaml").read_text())
        self.assertIn(f"{self.project}:/workspace:Z", cfg["services"]["agent"]["volumes"])
        self.assertTrue(cfg["x-metadata"]["no-git"])

    def test_selected_branch_clone_push_and_manual_fetch(self):
        initial = self.git(self.project, "rev-parse", "HEAD").stdout.strip()
        self.git(self.project, "checkout", "-qb", "chosen")
        (self.project / "source").write_text("chosen\n")
        self.git(self.project, "commit", "-qam", "chosen branch")
        self.git(self.project, "checkout", "-q", "main")
        self.share(branch="chosen")
        self.assertEqual(self.git(server.repository("workspace"), "branch", "--format=%(refname:short)").stdout.strip(), "chosen")
        self.assertFalse((self.session / "source.bundle").exists())
        self.assertFalse((self.session / "output.git").exists())
        clone.clone_repositories()
        self.assertEqual(self.git(clone.WORKSPACE, "branch", "--show-current").stdout.strip(), "chosen")
        self.assertEqual((clone.WORKSPACE / "source").read_text(), "chosen\n")
        self.assertEqual(self.git(clone.WORKSPACE, "remote", "get-url", "origin").stdout.strip(),
                         clone.GIT_URL + "workspace.git")
        self.git(clone.WORKSPACE, "checkout", "-qb", "agent-work")
        (clone.WORKSPACE / "source").write_text("agent\n")
        self.git(clone.WORKSPACE, "commit", "-qam", "agent work")
        # Even a server-side hook installed out of band cannot run through the helper.
        marker = self.root / "hook-ran"
        hook = server.repository("workspace") / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        hook.chmod(0o755)
        agent_url = self.agentbox["_git_url"](self.session, "workspace")
        self.git(clone.WORKSPACE, "remote", "set-url", "origin", agent_url)
        self.git(clone.WORKSPACE, "push", "-q", "origin", "HEAD")
        self.assertFalse(marker.exists())
        self.assertNotEqual(self.git(self.project, "show", f"{self.remote}/agent-work:source", check=False).returncode, 0)
        self.git(self.project, "fetch", self.remote)
        self.assertEqual(self.git(self.project, "show", f"{self.remote}/agent-work:source").stdout, "agent\n")
        self.assertEqual(self.git(self.project, "rev-parse", "HEAD").stdout.strip(), initial)
        self.assertEqual((self.project / "source").read_text(), "host\n")

    def test_sandbox_created_repository_can_publish_to_an_existing_shares_new_branch(self):
        self.share()
        created = self.root / "sandbox-created"
        created.mkdir()
        self.git(created, "init", "-q", "--initial-branch=main")
        (created / "new-project").write_text("created in the sandbox\n")
        self.git(created, "add", ".")
        self.git(created, "commit", "-qm", "new project")
        missing = self.git(created, "push", clone.GIT_URL + "new-project.git", "HEAD", check=False)
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(server.repository("new-project").exists())
        self.git(created, "push", clone.GIT_URL + "workspace.git", "HEAD:refs/heads/new-project")

        retrieved = self.root / "retrieved"
        url = self.git(self.project, "remote", "get-url", self.remote).stdout.strip()
        self.git(self.root, "clone", "-q", "--single-branch", "--branch", "new-project", url, str(retrieved))
        self.assertEqual((retrieved / "new-project").read_text(), "created in the sandbox\n")
        self.assertFalse((retrieved / "source").exists())
        self.assertEqual(self.git(retrieved, "rev-parse", "HEAD").stdout, self.git(created, "rev-parse", "HEAD").stdout)
        self.assertEqual(self.git(retrieved, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual((self.project / "source").read_text(), "host\n")

    def test_namespaced_mount_clone_push_and_manual_fetch(self):
        name = "org/repo"
        args = self.agentbox["build_parser"]().parse_args([
            "mount", str(self.project), "--as", name, "--session", self.session.name,
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            self.agentbox["cmd_mount"](args)
        self.assertIn("/context/git/org/repo", output.getvalue())
        self.assertEqual(server.repository(name), self.store / "repos" / "org" / "repo.git")
        self.assertTrue(server.repository(name).is_dir())
        self.assertEqual(self.agentbox["_git_repositories"](self.session), {name: str(self.project / ".git")})
        self.assertEqual(server.published_repositories(), [name])
        url = self.agentbox["_git_url"](self.session, name)
        self.assertTrue(url.endswith("#org/repo"))
        self.assertEqual(self.agentbox["_parse_git_url"](url), (self.session, name))
        self.assertEqual(self.git(self.project, "remote", "get-url", self.remote).stdout.strip(), url)

        self.assertEqual(clone.main([]), 0)
        target = clone.CONTEXT / name
        self.assertEqual((target / "source").read_text(), "host\n")
        self.assertEqual(self.git(target, "remote", "get-url", "origin").stdout.strip(), clone.GIT_URL + name + ".git")
        (target / "source").write_text("agent\n")
        self.git(target, "checkout", "-qb", "namespaced-work")
        self.git(target, "commit", "-qam", "namespaced agent work")
        self.git(target, "push", "-q", "origin", "HEAD")
        self.git(self.project, "fetch", self.remote)
        self.assertEqual(self.git(self.project, "show", f"{self.remote}/namespaced-work:source").stdout, "agent\n")
        self.assertEqual((self.project / "source").read_text(), "host\n")

    def test_namespaced_shares_keep_same_basename_separate(self):
        names = ("org/repo", "other/repo", "org/team/repo")
        for name in names:
            path = self.root / "git" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            self.git(self.root, "clone", "-q", str(self.project), str(path))
            self.share(name, path)
        # Named clones bypass the catalog, including names with several components.
        with patch.object(clone, "shared_repositories", side_effect=AssertionError("unexpected catalog request")):
            self.assertEqual(clone.main(list(names)), 0)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(server.repository(name).is_dir())
                self.assertEqual((clone.CONTEXT / name / "source").read_text(), "host\n")
                self.assertEqual(self.git(clone.CONTEXT / name, "remote", "get-url", "origin").stdout.strip(),
                                 clone.GIT_URL + name + ".git")
        self.assertEqual(server.published_repositories(), sorted(names))

    def test_startup_clone_and_explicit_clone_of_later_shares_preserve_existing_work(self):
        self.share()
        self.assertEqual(clone.main([]), 0)
        (clone.WORKSPACE / "source").write_text("uncommitted agent work\n")
        reference = self.root / "library"
        self.git(self.root, "clone", "-q", str(self.project), str(reference))
        self.share("library", reference)
        self.assertFalse((clone.CONTEXT / "library").exists())
        # A named clone is explicit and does not query the catalog.
        with patch.object(clone, "shared_repositories", side_effect=AssertionError("unexpected catalog request")):
            self.assertEqual(clone.main(["library"]), 0)
        self.assertTrue((clone.CONTEXT / "library" / ".git").is_dir())
        self.assertEqual(clone.main([]), 0)
        self.assertEqual((clone.WORKSPACE / "source").read_text(), "uncommitted agent work\n")
        self.assertEqual(len(self.agentbox["_git_repositories"](self.session)), 2)

    def test_clone_helper_runs_once_without_background_retries(self):
        with (patch.object(clone, "clone_repositories") as run,
              patch.object(clone.time, "sleep") as sleep):
            self.assertEqual(clone.main([]), 0)
            run.assert_called_once_with([])
            sleep.assert_not_called()
        with (patch.object(clone, "clone_repositories", side_effect=OSError("unavailable")) as run,
              patch.object(clone.time, "sleep") as sleep, redirect_stderr(io.StringIO())):
            self.assertEqual(clone.main([]), 1)
            run.assert_called_once_with([])
            sleep.assert_not_called()

    def test_startup_clone_wait_is_bounded_and_stops_after_success(self):
        with (patch.object(clone, "clone_repositories", side_effect=[OSError("starting"), None]) as run,
              patch.object(clone.time, "monotonic", side_effect=[0, 0]),
              patch.object(clone.time, "sleep") as sleep, redirect_stderr(io.StringIO())):
            self.assertEqual(clone.main(["--wait", "2"]), 0)
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(2)
        with (patch.object(clone, "clone_repositories", side_effect=OSError("unavailable")) as run,
              patch.object(clone.time, "monotonic", side_effect=[0, 0, 2]),
              patch.object(clone.time, "sleep") as sleep, redirect_stderr(io.StringIO())):
            self.assertEqual(clone.main(["--wait", "2"]), 1)
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(2)

    def test_clone_helper_preserves_nonempty_destinations_and_validates_names(self):
        self.share("library")
        target = clone.CONTEXT / "library"
        target.mkdir(parents=True)
        (target / "keep").write_text("uncommitted\n")
        self.assertEqual(clone.main(["library"]), 0)
        self.assertEqual((target / "keep").read_text(), "uncommitted\n")
        self.assertFalse((target / ".git").exists())
        for name in ("../escape", "/absolute", "-option", "UPPER"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                clone.clone_repositories([name])

    def test_detached_head_and_bare_repository(self):
        self.git(self.project, "checkout", "-q", "--detach")
        commit = self.git(self.project, "rev-parse", "HEAD").stdout.strip()
        self.share()
        self.assertEqual(self.git(server.repository("workspace"), "symbolic-ref", "HEAD").stdout.strip(),
                         "refs/heads/snapshot-" + commit[:12])
        self.assertEqual(server.published_repositories(), ["workspace"])
        bare = self.root / "bare.git"
        self.git(self.root, "clone", "--bare", "-q", str(self.project), str(bare))
        self.share("bare", bare)
        self.assertTrue(server.repository("bare").is_dir())

    def test_sha256_repository(self):
        path = self.root / "sha256"
        self.git(self.root, "init", "-q", "--object-format=sha256", "--initial-branch=main", str(path))
        self.git(path, "commit", "--allow-empty", "-qm", "sha256")
        self.share("sha256", path)
        self.assertEqual(self.git(server.repository("sha256"), "rev-parse", "--show-object-format").stdout.strip(), "sha256")

    def test_linked_git_worktree(self):
        worktree = self.root / "linked-worktree"
        self.git(self.project, "worktree", "add", "-qb", "linked", str(worktree))
        self.share("org/linked", worktree)
        clone.clone_repositories()
        self.assertEqual(self.git(clone.CONTEXT / "org/linked", "branch", "--show-current").stdout.strip(), "linked")
        self.assertEqual(self.agentbox["_git_repositories"](self.session), {"org/linked": str(self.project / ".git")})
        self.share(None, self.project)
        self.assertEqual(self.agentbox["_git_repositories"](self.session), {"org/linked": str(self.project / ".git")})

    def test_worktrees_share_one_remote_and_publish_independent_branches(self):
        self.share()
        clone.clone_repositories()
        worktree = self.root / "linked-worktree"
        self.git(self.project, "worktree", "add", "-qb", "linked", str(worktree))
        (worktree / "source").write_text("linked\n")
        self.git(worktree, "commit", "-qam", "linked work")
        # Mounting from another worktree reuses the existing sidecar repository.
        self.share(None, worktree)
        self.assertEqual(self.git(worktree, "remote", "get-url", self.remote).stdout,
                         self.git(self.project, "remote", "get-url", self.remote).stdout)
        self.assertEqual(self.agentbox["_git_repositories"](self.session), {"workspace": str(self.project / ".git")})
        self.assertEqual(self.git(server.repository("workspace"), "show", "linked:source").stdout, "linked\n")
        # Further branches need only ordinary Git, from either worktree.
        self.git(self.project, "branch", "another")
        self.git(self.project, "push", self.remote, "another")
        self.assertEqual(set(self.git(server.repository("workspace"), "branch", "--format=%(refname:short)").stdout.splitlines()),
                         {"main", "linked", "another"})
        with patch.dict(self.g, find_project_dir=lambda: worktree), redirect_stdout(io.StringIO()):
            self.agentbox["cmd_remote_cleanup"](types.SimpleNamespace())
        self.assertIn(self.remote, self.git(self.project, "remote").stdout.splitlines())
        clone.clone_repositories()
        self.assertEqual(self.git(clone.WORKSPACE, "branch", "--show-current").stdout.strip(), "main")

    def test_mount_resolves_once_and_does_not_rewrite_compose(self):
        compose = self.session / "compose.yaml"
        compose.write_text(compose.read_text() + "\n# Keep user comments and formatting.\n")
        before = compose.read_bytes()
        resolve = Mock(wraps=self.g["_repository_revision"])
        args = types.SimpleNamespace(repo=str(self.project), repo_name="library", branch="", name=None,
                                     session=self.session.name)
        with patch.dict(self.g, _repository_revision=resolve), redirect_stdout(io.StringIO()):
            self.agentbox["cmd_mount"](args)
        resolve.assert_called_once()
        self.assertEqual(compose.read_bytes(), before)
        registry = self.session / "git-repositories.json"
        self.assertEqual(json.loads(registry.read_text()), {"library": str(self.project / ".git")})
        self.assertNotIn("git-repositories", yaml.safe_load(before)["x-metadata"])
        self.agentbox["generate_compose"](self.session, self.project, "feature", 9876, {}, {}, [])
        self.assertEqual(self.agentbox["_git_repositories"](self.session), {"library": str(self.project / ".git")})

    def test_remote_helper_delegates_to_sibling_cli_and_preserves_streams(self):
        standalone = self.root / "helper-only"
        standalone.mkdir()
        shutil.copy2(ROOT / "bin" / "git-remote-agentbox", standalone / "git-remote-agentbox")
        url = self.agentbox["_git_url"](self.session, "workspace")
        (standalone / "agentbox").write_text(f"""import sys
assert sys.argv[1:] == ['_git-remote-helper', {url!r}], sys.argv
sys.stdout.buffer.write(sys.stdin.buffer.read())
sys.stderr.write('CLI diagnostic\\n')
sys.exit(23)
""")
        symlink = self.root / "git-remote-agentbox-stage"
        symlink.symlink_to(standalone / "git-remote-agentbox")
        payload = b"binary pack data\x00\xff\n"
        result = subprocess.run([str(symlink), self.remote, url], input=payload, capture_output=True)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout, payload)
        self.assertEqual(result.stderr, b"CLI diagnostic\n")

    def test_git_url_uses_nonempty_image_tag_and_parses_without_wrapper_environment(self):
        for tag in (None, "", "stage", "latest", "test_1.2-rc"):
            with self.subTest(tag=tag), patch.dict(os.environ):
                if tag is None:
                    os.environ.pop("AGENTBOX_IMAGE_TAG", None)
                else:
                    os.environ["AGENTBOX_IMAGE_TAG"] = tag
                url = self.agentbox["_git_url"](self.session, "org/library")
                transport = f"agentbox-{tag}" if tag else "agentbox"
                self.assertEqual(url, f"{transport}::{self.session.as_uri()}#org/library")
                os.environ["AGENTBOX_IMAGE_TAG"] = "other"
                self.assertEqual(self.agentbox["_parse_git_url"](url), (self.session, "org/library"))
                self.assertEqual(self.agentbox["_parse_git_url"](url.partition("::")[2]),
                                 (self.session, "org/library"))

    def test_tagged_host_transport_ignores_agentbox_wrapper_environment(self):
        stage = self.root / "stage installation"
        (stage / "bin").mkdir(parents=True)
        for name in ("agentbox", "git-remote-agentbox"):
            shutil.copy2(ROOT / "bin" / name, stage / "bin" / name)
        (stage / "proxy").symlink_to(ROOT / "proxy")
        (self.fake_bin / "git-remote-agentbox-stage").symlink_to(stage / "bin" / "git-remote-agentbox")
        with patch.dict(os.environ, AGENTBOX_IMAGE_TAG="stage"):
            self.share()
        url = self.git(self.project, "remote", "get-url", self.remote).stdout
        self.assertTrue(url.startswith("agentbox-stage::"))
        variables = ("AGENTBOX_HOME", "AGENTBOX_STATE", "AGENTBOX_IMAGE_TAG",
                     "AGENTBOX_PRESET", "XDG_STATE_HOME")
        foreign = {
            "AGENTBOX_HOME": str(self.root / "other-installation"),
            "AGENTBOX_STATE": str(self.root / "other-state"),
            "AGENTBOX_IMAGE_TAG": "other-tag",
            "AGENTBOX_PRESET": "other-preset",
            "XDG_STATE_HOME": str(self.root / "other-xdg-state"),
        }
        for index, settings in enumerate(({}, foreign)):
            with self.subTest(environment=settings), patch.dict(os.environ):
                for variable in variables:
                    os.environ.pop(variable, None)
                os.environ.update(settings)
                self.git(self.project, "commit", "--allow-empty", "-qm", f"host update {index}")
                commit = self.git(self.project, "rev-parse", "HEAD").stdout.strip()
                self.git(self.project, "push", self.remote, f"HEAD:published-{index}")
                stored = server.repository("workspace")
                self.assertEqual(self.git(stored, "rev-parse", f"published-{index}").stdout.strip(), commit)
                self.git(stored, "update-ref", f"refs/heads/reply-{index}", commit)
                self.git(self.project, "fetch", self.remote)
                self.assertEqual(self.git(self.project, "rev-parse", f"{self.remote}/reply-{index}").stdout.strip(), commit)
                self.assertEqual(self.git(self.project, "remote", "get-url", self.remote).stdout, url)
                self.assertNotEqual(self.git(self.project, "config", "--get",
                                            f"remote.{self.remote}.vcs", check=False).returncode, 0)

    def test_internal_git_helper_cli_capabilities(self):
        url = self.agentbox["_git_url"](self.session, "workspace")
        result = subprocess.run([sys.executable, str(ROOT / "bin" / "agentbox"), "_git-remote-helper", url],
                                input=b"capabilities\noption progress true\n", capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"connect\n\nunsupported\n")
        self.assertEqual(result.stderr, b"")

    def test_host_git_transfers_do_not_acquire_secret_sync_lock_or_restart_internal_pushes(self):
        self.share()
        url = self.agentbox["_git_url"](self.session, "workspace")
        for service in ("git-upload-pack", "git-receive-pack"):
            for already_started in (False, True):
                with self.subTest(service=service, already_started=already_started):
                    start = Mock()
                    lock = Mock(side_effect=AssertionError("Git acquired the secret synchronization lock"))
                    stdin = types.SimpleNamespace(buffer=io.BytesIO(f"connect {service}\n".encode()))
                    output = io.StringIO()
                    with (patch.dict(self.g, _secret_sync_lock=lock, _start_git=start),
                          patch.dict(os.environ, AGENTBOX_GIT_STARTED=str(self.session.resolve()) if already_started else ""),
                          patch.object(sys, "stdin", stdin), redirect_stdout(output),
                          patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 23)) as transfer):
                        self.assertEqual(self.agentbox["git_remote_helper"](url), 23)
                    lock.assert_not_called()
                    if already_started:
                        start.assert_not_called()
                    else:
                        start.assert_called_once_with(self.session)
                    transfer.assert_called_once()
                    self.assertEqual(transfer.call_args.args[0][-2:],
                                     [service.removeprefix("git-"), "/srv/git/repos/workspace.git"])
                    self.assertEqual(output.getvalue(), "\n")

    def test_git_start_refuses_removed_sessions(self):
        (self.session / "compose.yaml").unlink()
        compose = Mock()
        with (patch.dict(self.g, compose=compose), redirect_stderr(io.StringIO()),
              self.assertRaises(SystemExit)):
            self.agentbox["_start_git"](self.session)
        compose.assert_not_called()

    def test_internal_git_helper_cli_errors_use_stderr(self):
        for arguments, message in (([], b"usage:"), (["file:///session#repo", "extra"], b"usage:"),
                                   (["https://host/session#repo"], b"Invalid agentbox Git URL")):
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(ROOT / "bin" / "agentbox"),
                                         "_git-remote-helper", *arguments], capture_output=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b"")
                self.assertIn(message, result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)

    def test_invalid_registrations_fail_without_rewriting_compose(self):
        before = (self.session / "compose.yaml").read_bytes()
        for content in ('{', '[]', '{"../escape": "/repo/.git"}', '{"repo": "relative/.git"}'):
            with self.subTest(content=content), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                (self.session / "git-repositories.json").write_text(content)
                self.agentbox["_git_repositories"](self.session)
        self.assertEqual((self.session / "compose.yaml").read_bytes(), before)

    def test_non_git_project_does_not_implicitly_get_a_writable_mount(self):
        project = self.root / "not-a-repository"
        project.mkdir()
        args = types.SimpleNamespace(dir=str(project), name="empty", persist=False, preset="default", no_git=False,
                                     branch="", ro_mounts=[], rw_mounts=[], start=False)
        with redirect_stdout(io.StringIO()):
            self.agentbox["cmd_init"](args)
        session = self.root / "state" / "sessions" / self.agentbox["_session_id"]("empty", project)
        cfg = yaml.safe_load((session / "compose.yaml").read_text())
        self.assertEqual(self.agentbox["_git_repositories"](session), {})
        self.assertFalse(any(":/workspace:" in mount for mount in cfg["services"]["agent"]["volumes"]))

    def test_conflicting_remote_and_repository_name_are_not_overwritten(self):
        self.git(self.project, "remote", "add", self.remote, "https://example.invalid/original")
        with self.assertRaises(SystemExit):
            self.share()
        self.assertEqual(self.events, [])
        self.assertEqual(self.git(self.project, "remote", "get-url", self.remote).stdout.strip(),
                         "https://example.invalid/original")
        self.git(self.project, "remote", "remove", self.remote)
        self.share()
        other = self.root / "other"
        self.git(self.root, "clone", "-q", str(self.project), str(other))
        with self.assertRaises(SystemExit):
            self.share(path=other)

    def test_non_fast_forward_share_does_not_republish_or_overwrite_agent_work(self):
        self.share()
        remote = server.repository("workspace")
        self.git(remote, "update-ref", "refs/heads/main", self.git(self.project, "rev-parse", "HEAD").stdout.strip())
        self.git(self.project, "checkout", "--orphan", "unrelated", "-q")
        self.git(self.project, "commit", "--allow-empty", "-qm", "unrelated")
        self.git(self.project, "branch", "-f", "main", "HEAD")
        marker = remote / "git-daemon-export-ok"
        before = marker.stat().st_mtime_ns
        with self.assertRaises(subprocess.CalledProcessError):
            self.share(branch="main")
        self.assertEqual(marker.stat().st_mtime_ns, before)

    def test_path_validation_and_reserved_bind_mount_destination(self):
        for name in ("a", "a" * 100, "org/repo", "org/team/repo", "org.example/lib_name-2", "org/" + "a" * 96):
            with self.subTest(name=name):
                self.assertIsNotNone(self.agentbox["GIT_REPOSITORY_NAME"].fullmatch(name))
                self.assertIsNotNone(clone.NAME.fullmatch(name))
                self.assertEqual(server.repository(name), self.store / "repos" / (name + ".git"))
                self.assertEqual(self.agentbox["_parse_git_url"](self.agentbox["_git_url"](self.session, name)),
                                 (self.session, name))
        for name in ("", ".", "..", "../escape", "/absolute", "org/../escape", "org/./repo", "org//repo",
                     "org/repo/", "org/.git", "org/-option", "org/UPPER", "org\\repo", "org/%2e%2e/repo",
                     "-option", "UPPER", "a" * 101, "org/" + "a" * 97):
            with self.subTest(name=name), self.assertRaises(ValueError):
                server.repository(name)
            with self.subTest(name=name), self.assertRaises(SystemExit):
                self.share(name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                clone.clone_repositories([name])
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.agentbox["_parse_git_url"](self.agentbox["_git_url"](self.session, name))
        for name in ("git", "git/shadow", "../escape", "/absolute"):
            with self.subTest(name=name), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.agentbox["_validate_context_destination"](name)
        for url in ("file:///session#../escape", "https://host/session#repo", "file://host/session#repo",
                    "agentbox-stage::file://host/session#repo", "other::file:///session#repo"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.agentbox["_parse_git_url"](url)

    def test_catalog_http_is_read_only_and_cannot_read_repository_config(self):
        self.share()

        class Connection:
            def __init__(self, method, path):
                self.request = io.BytesIO(f"{method} {path} HTTP/1.0\r\n\r\n".encode())
                self.response = bytearray()

            def makefile(self, *args):
                return self.request

            def sendall(self, data):
                self.response.extend(data)

        def request(path, method="GET"):
            connection = Connection(method, path)
            server.CatalogHandler(connection, ("127.0.0.1", 1), None)
            headers, body = bytes(connection.response).split(b"\r\n\r\n", 1)
            return int(headers.split()[1]), body

        status, body = request("/manifest.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), ["workspace"])
        for path in ("/repos/workspace.git/config", "/../dotfiles.json", "/manifest.json?path=/etc/passwd"):
            with self.subTest(path=path):
                self.assertEqual(request(path)[0], 404)
        self.assertEqual(request("/manifest.json", "POST")[0], 501)
        # Every request reflects current markers without a watcher or refresh.
        self.unmount("workspace")
        self.assertEqual(json.loads(request("/manifest.json")[1]), [])
        self.share()
        self.assertEqual(json.loads(request("/manifest.json")[1]), ["workspace"])

    def test_remove_cleans_all_registered_repos_but_preserves_repointed_remotes(self):
        self.share()
        with patch.dict(os.environ, AGENTBOX_IMAGE_TAG="stage"):
            self.git(self.project, "remote", "set-url", self.remote,
                     self.agentbox["_git_url"](self.session, "workspace"))
        reference = self.root / "library"
        self.git(self.root, "clone", "-q", str(self.project), str(reference))
        self.share("org/library", reference)
        self.git(reference, "remote", "set-url", self.remote, "https://example.invalid/repointed")
        with (patch.dict(self.g, resolve_session=lambda args: (self.session, self.project)),
              redirect_stdout(io.StringIO())):
            self.agentbox["cmd_remove"](types.SimpleNamespace())
        self.assertFalse(self.session.exists())
        self.assertNotIn(self.remote, self.git(self.project, "remote").stdout.splitlines())
        self.assertIn(self.remote, self.git(reference, "remote").stdout.splitlines())

    def test_remote_cleanup_recognizes_a_foreign_sessions_reference_share(self):
        reference = self.root / "library"
        self.git(self.root, "clone", "-q", str(self.project), str(reference))
        self.share("org/library", reference)
        with patch.dict(os.environ, AGENTBOX_IMAGE_TAG="stage"):
            self.git(reference, "remote", "set-url", self.remote,
                     self.agentbox["_git_url"](self.session, "org/library"))
        with patch.dict(self.g, find_project_dir=lambda: reference), redirect_stdout(io.StringIO()):
            self.agentbox["cmd_remote_cleanup"](types.SimpleNamespace())
            self.assertIn(self.remote, self.git(reference, "remote").stdout.splitlines())
            (self.session / "compose.yaml").unlink()
            self.agentbox["cmd_remote_cleanup"](types.SimpleNamespace())
        self.assertNotIn(self.remote, self.git(reference, "remote").stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
