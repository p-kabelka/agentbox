"""Internal-only Git daemon and read-only repository catalog; management is exec-only."""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STORE = Path("/srv/git")
NAME = re.compile(r"(?=.{1,100}\Z)[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*", re.ASCII)


def repository(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("Invalid repository name")
    return STORE / "repos" / (name + ".git")


def repository_roots():
    """Walk namespaces without descending into bare repositories."""
    for root, directories, _ in os.walk(STORE / "repos"):
        path = Path(root)
        if path.name.endswith(".git") and (path / "HEAD").is_file() and (path / "objects").is_dir():
            directories.clear()
            yield path


def published_repositories():
    repos = STORE / "repos"
    return sorted(path.relative_to(repos).as_posix().removesuffix(".git")
                  for path in repository_roots() if (path / "git-daemon-export-ok").is_file())


def git(*args, **kwargs):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], check=True, **kwargs)


def check_branch(branch):
    git("check-ref-format", "refs/heads/" + branch, stdout=subprocess.DEVNULL)


def initialize(name, branch, object_format):
    path = repository(name)
    check_branch(branch)
    if object_format not in ("sha1", "sha256"):
        raise ValueError("Unsupported Git object format")
    for existing in repository_roots():
        if existing != path and (path.is_relative_to(existing) or existing.is_relative_to(path)):
            shared = existing.relative_to(STORE / "repos").as_posix().removesuffix(".git")
            raise ValueError(f"Repository '{name}' overlaps repository '{shared}'")
    if path.exists():
        actual = git("-C", str(path), "rev-parse", "--show-object-format",
                     capture_output=True, text=True).stdout.strip()
        if actual != object_format:
            raise ValueError("Repository object format differs")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    git("init", "--bare", "--quiet", "--template=", "--initial-branch=" + branch,
        "--object-format=" + object_format, str(path))


def publish(name, branch):
    path = repository(name)
    check_branch(branch)
    git("-C", str(path), "rev-parse", "--verify", "refs/heads/" + branch + "^{commit}",
        stdout=subprocess.DEVNULL)
    git("-C", str(path), "symbolic-ref", "HEAD", "refs/heads/" + branch)
    (path / "git-daemon-export-ok").touch()


def remove_repository(name):
    path = repository(name)
    (path / "git-daemon-export-ok").unlink(missing_ok=True)
    if path.exists():
        shutil.rmtree(path)


class CatalogHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # No filesystem paths, repository creation, or writes are exposed over HTTP.
        if self.path != "/manifest.json":
            self.send_error(404)
            return
        payload = json.dumps(published_repositories()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def serve():
    (STORE / "repos").mkdir(parents=True, exist_ok=True)
    http = ThreadingHTTPServer(("0.0.0.0", 8083), CatalogHandler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    daemon = subprocess.Popen([
        "git", "-c", "core.hooksPath=/dev/null", "daemon", "--reuseaddr",
        "--base-path=" + str(STORE / "repos"), "--enable=receive-pack",
        "--forbid-override=receive-pack", "--listen=0.0.0.0", str(STORE / "repos"),
    ])

    def stop(*args):
        daemon.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return daemon.wait()
    finally:
        http.shutdown()
        http.server_close()


def main():
    args = sys.argv[1:]
    try:
        if not args:
            return serve()
        if len(args) == 4 and args[0] == "init":
            initialize(*args[1:])
        elif len(args) == 3 and args[0] == "publish":
            publish(*args[1:])
        elif len(args) == 2 and args[0] == "remove":
            remove_repository(args[1])
        else:
            raise ValueError("Invalid Git management operation")
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Git sidecar: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
