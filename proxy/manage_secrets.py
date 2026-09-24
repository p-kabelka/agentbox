"""One-shot, stdin-only provider secret installation into the proxy tmpfs."""

import hashlib
import http.client
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "addons"))
sys.path.insert(0, "/addons")
from secret_contract import MANAGED_TARGET, NAME_MAX, validate_target

SECRET_DIR = Path("/run/secrets")
TEMP_PREFIX = ".agentbox-secret-"
RELOAD_PORT = 8082


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


def install(target: str, stream, *, require_eof: bool = True) -> None:
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
            if (require_eof and stream.read(1)) or digest.hexdigest().encode() != expected:
                raise ValueError("Installation frame verification failed")
            output.flush()
            os.fchmod(output.fileno(), 0o400)
        os.replace(temporary, SECRET_DIR / target)
    finally:
        os.umask(old_umask)
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _request(path: str, *, expected: str = "", timeout: int = 2) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", RELOAD_PORT, timeout=timeout)
    headers = {"X-Agentbox-Config-Fingerprint": expected} if expected else {}
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        status = response.status
        body = json.loads(response.read())
        if not isinstance(body, dict):
            raise ValueError("Invalid proxy response")
        return status, body
    finally:
        connection.close()


def wait_ready() -> None:
    deadline = time.monotonic() + 80
    while True:
        try:
            status, body = _request("/health")
            if status == 200 and body.get("ready") is True:
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Proxy reload interface timed out")
        time.sleep(min(0.2, remaining))


def sync(stream) -> dict:
    """Receive one snapshot, replace managed secrets, and reload in the same exec."""
    header = re.fullmatch(rb"v1 ([0-9a-f]{64}) (0|[1-9][0-9]*)\n", stream.readline(128))
    if header is None:
        raise ValueError("Invalid synchronization header")
    expected, count = header.groups()
    reset()
    targets = set()
    for _ in range(int(count)):
        line = stream.readline(NAME_MAX + 2)
        if not line.endswith(b"\n"):
            raise ValueError("Invalid synchronization target")
        target = line[:-1].decode("ascii")
        validate_target(target)
        if target in targets:
            raise ValueError("Duplicate synchronization target")
        targets.add(target)
        install(target, stream, require_eof=False)
    if stream.read(1):
        raise ValueError("Trailing synchronization data")
    wait_ready()  # The proxy can start mitmweb while we stage credentials.
    status, body = _request("/reload/providers", expected=expected.decode(), timeout=120)
    return {"status": status, "body": body}


def main() -> int:
    try:
        if sys.argv[1:] != ["sync"]:
            raise ValueError("Invalid secret manager operation")
        print(json.dumps(sync(sys.stdin.buffer)))
        return 0
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        print("Secret synchronization failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
