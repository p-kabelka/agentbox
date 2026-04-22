import logging
import os
import threading
from abc import ABC, abstractmethod
from pathlib import PurePosixPath

log = logging.getLogger("proxy")

RESOLVER_CLASSES: dict[str, type["CredentialResolver"]] = {}


class CredentialResolver(ABC):
    def __init__(self, config: dict):
        pass

    @abstractmethod
    def resolve(self) -> str | None: ...

    def __init_subclass__(cls, resolver_type: str = "", **kwargs):
        super().__init_subclass__(**kwargs)
        if resolver_type:
            RESOLVER_CLASSES[resolver_type] = cls


class StaticKeyResolver(CredentialResolver, resolver_type="static"):
    def __init__(self, config: dict):
        self._key = ""
        name = config.get("name", "?")
        key_file = config.get("api_key_file", "")
        env_var = config.get("api_key_env", "")
        if key_file:
            container_path = f"/run/secrets/{PurePosixPath(key_file).name}"
            try:
                with open(container_path) as f:
                    self._key = f.read().strip()
            except OSError as exc:
                log.error("Provider '%s': cannot read key file at '%s' (from api_key_file '%s'): %s",
                          name, container_path, key_file, exc)
        if not self._key and env_var:
            self._key = os.environ.get(env_var, "").strip()
        if not self._key and (key_file or env_var):
            log.warning("Provider '%s' enabled but no API key found (file=%s, env=%s)",
                        name, key_file or "unset", env_var or "unset")

    def resolve(self) -> str | None:
        return self._key or None


class OAuthResolver(CredentialResolver, resolver_type="oauth"):
    def __init__(self, config: dict):
        self._creds = None
        self._lock = threading.Lock()
        self._load_creds()

    def _load_creds(self) -> None:
        try:
            import google.auth
            import google.auth.transport.requests
            cenv = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
            if cenv and not os.path.isfile(cenv):
                del os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            self._creds = (creds, google.auth.transport.requests.Request())
            log.info("Loaded Google credentials for OAuth injection (type=%s)",
                     type(creds).__name__)
        except Exception as exc:
            log.error("Failed to load Google credentials: %s", exc)

    def resolve(self) -> str | None:
        if not self._creds:
            return None
        creds, req = self._creds
        with self._lock:
            if not creds.valid:
                creds.refresh(req)
            return creds.token
