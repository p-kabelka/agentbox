"""Wire and naming contracts shared by the host and proxy (standard library only)."""

import hashlib
import json
import re
from pathlib import PurePosixPath

MANAGED_TARGET = re.compile(r"[0-9a-f]{12}-[A-Za-z0-9._-]+", re.ASCII)
NAME_MAX = 255


def validate_target(target: str) -> None:
    if (not MANAGED_TARGET.fullmatch(target) or len(target) > NAME_MAX
            or target[13:] in {".", ".."}):
        raise ValueError("Invalid provider secret target name")


def injection_secret_name(source: str) -> str:
    if not isinstance(source, str) or not source:
        raise ValueError("api_key_file must be a non-empty string")
    basename = PurePosixPath(source).name
    if source.rstrip("/").rsplit("/", 1)[-1] in {".", ".."}:
        raise ValueError("Invalid api_key_file basename")
    target = f"{hashlib.sha256(source.encode()).hexdigest()[:12]}-{basename}"
    validate_target(target)
    return target


def fingerprint(value) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def installation_frame(payload: bytes) -> bytes:
    return f"{len(payload)} {hashlib.sha256(payload).hexdigest()}\n".encode() + payload


def synchronization_frame(expected: str, sources: list[tuple[str, bytes]]) -> bytes:
    """One stdin transaction: snapshot fingerprint, targets, and verified payload frames."""
    for target, _ in sources:
        validate_target(target)
    header = f"v1 {expected} {len(sources)}\n".encode()
    return header + b"".join(target.encode() + b"\n" + installation_frame(payload)
                             for target, payload in sources)
