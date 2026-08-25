import base64
import json
import logging
import os
import threading
import time
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


_CURSOR_EXCHANGE_URL = "https://api2.cursor.sh/auth/exchange_user_api_key"
_REFRESH_MARGIN = 300


class CursorApiKeyResolver(CredentialResolver, resolver_type="cursor_api_key"):
    def __init__(self, config: dict):
        self._api_key = ""
        self._token: str | None = None
        self._token_exp: float = 0
        self._lock = threading.Lock()

        name = config.get("name", "?")
        key_file = config.get("api_key_file", "")
        env_var = config.get("api_key_env", "")
        if key_file:
            container_path = f"/run/secrets/{PurePosixPath(key_file).name}"
            try:
                with open(container_path) as f:
                    self._api_key = f.read().strip()
            except OSError as exc:
                log.error("Provider '%s': cannot read key file at '%s' (from api_key_file '%s'): %s",
                          name, container_path, key_file, exc)
        if not self._api_key and env_var:
            self._api_key = os.environ.get(env_var, "").strip()
        if not self._api_key and (key_file or env_var):
            log.warning("Provider '%s' enabled but no API key found (file=%s, env=%s)",
                        name, key_file or "unset", env_var or "unset")

    def _exchange(self) -> None:
        import requests

        try:
            resp = requests.post(
                _CURSOR_EXCHANGE_URL,
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                json={},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            access_token = data.get("accessToken", "")
            if not access_token:
                log.error("Cursor token exchange returned no accessToken")
                return
            self._token = access_token
            self._token_exp = self._parse_jwt_exp(access_token)
            log.info("Cursor token exchanged successfully (expires at %s)",
                     time.strftime("%H:%M:%S", time.gmtime(self._token_exp)))
        except Exception as exc:
            log.error("Cursor token exchange failed: %s", exc)

    @staticmethod
    def _parse_jwt_exp(token: str) -> float:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        return float(claims["exp"])

    def resolve(self) -> str | None:
        if not self._api_key:
            return None
        with self._lock:
            if self._token is None or time.time() >= self._token_exp - _REFRESH_MARGIN:
                self._exchange()
            return self._token


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
