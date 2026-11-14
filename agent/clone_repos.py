#!/usr/bin/env python3
"""Clone shared repositories once, at startup or on explicit request."""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CATALOG = "http://git:8083/manifest.json"
GIT_URL = "git://git/"
WORKSPACE = Path("/workspace")
CONTEXT = Path("/context/git")
NAME = re.compile(r"(?=.{1,100}\Z)[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*", re.ASCII)
CLIENT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def shared_repositories():
    with CLIENT.open(CATALOG, timeout=5) as response:
        names = json.load(response)
    if not isinstance(names, list):
        raise ValueError("Invalid repository catalog")
    return names


def clone_repositories(names=()):
    for name in names or shared_repositories():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError("Invalid repository name")
        target = WORKSPACE if name == "workspace" else CONTEXT / name
        # Never pull, reset, switch branches, or overwrite an existing directory.
        if (target / ".git").exists():
            continue
        if target.exists() and any(target.iterdir()):
            print(f"[agent] Not cloning {name}: {target} is not empty", file=sys.stderr)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"[agent] Cloning {name} into {target}...", flush=True)
        subprocess.run(["git", "clone", "--quiet", "--",
                        GIT_URL + name + ".git", str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "config", "push.autoSetupRemote", "true"], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agentbox-clone", description=__doc__)
    parser.add_argument("names", nargs="*", metavar="NAME", help="Repositories to clone, e.g. org/repo (default: all shared repositories)")
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                        help="Retry failures during startup for SECONDS (default: no retries)")
    args = parser.parse_args(argv)
    deadline = time.monotonic() + args.wait
    while True:
        try:
            clone_repositories(args.names)
            return 0
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            print(f"[agent] Cloning shared repositories: {exc}", file=sys.stderr, flush=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 1
            time.sleep(min(2, remaining))


if __name__ == "__main__":
    sys.exit(main())
