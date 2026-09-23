"""One-shot, stdin-only provider secret installation into the proxy tmpfs."""

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "addons"))
sys.path.insert(0, "/addons")
from secret_contract import MANAGED_TARGET, validate_target

SECRET_DIR = Path("/run/secrets")
TEMP_PREFIX = ".agentbox-secret-"


def inventory() -> list[Path]:
    entries = list(SECRET_DIR.iterdir())
    for entry in entries:
        if not (MANAGED_TARGET.fullmatch(entry.name) or entry.name.startswith(TEMP_PREFIX)):
            raise ValueError("Unrelated entry in /run/secrets; reset refused")
        if not stat.S_ISREG(entry.lstat().st_mode):
            raise ValueError("Non-regular entry in /run/secrets; reset refused")
    return entries


def reset() -> None:
    for entry in inventory():
        entry.unlink()


def install(target: str, stream) -> None:
    validate_target(target)
    header = stream.readline(128)
    if not re.fullmatch(rb"[1-9][0-9]* [0-9a-f]{64}\n", header):
        raise ValueError("Invalid installation frame")
    length, expected = header.split()
    remaining = int(length)
    digest = hashlib.sha256()
    temporary = None
    old_umask = os.umask(0o077)
    try:
        fd, temporary = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=SECRET_DIR)
        with os.fdopen(fd, "wb") as output:
            while remaining:
                chunk = stream.read(min(remaining, 65536))
                if not chunk:
                    raise ValueError("Incomplete installation frame")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            if stream.read(1) or digest.hexdigest().encode() != expected:
                raise ValueError("Installation frame verification failed")
            output.flush()
            os.fchmod(output.fileno(), 0o400)
        os.replace(temporary, SECRET_DIR / target)
    finally:
        os.umask(old_umask)
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def main() -> int:
    try:
        args = sys.argv[1:]
        if args == ["inventory"]:
            print(json.dumps([entry.name for entry in inventory()]))
        elif args == ["reset"]:
            reset()
        elif len(args) == 2 and args[0] == "install":
            install(args[1], sys.stdin.buffer)
        else:
            raise ValueError("Invalid secret manager operation")
        return 0
    except (OSError, ValueError):
        print("Secret store operation failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
