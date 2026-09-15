"""Where the uploader keeps its settings.

The site address and history of sent tournaments live in
`%APPDATA%\\EventUploader\\config.json`. The token goes to the Windows Credential
Manager through `keyring`; only if that is unavailable does it fall back to the
config file, and the window says so.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import keyring
except ImportError:  # pragma: no cover - depends on the machine
    keyring = None

SERVICE = "EventUploader"


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / "EventUploader"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Settings:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (config_dir() / "config.json")
        self.data: Dict[str, Any] = {"site_url": "", "cdp_port": 9222, "sent": {}}
        self.token_in_file = False
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    @property
    def site_url(self) -> str:
        return str(self.data.get("site_url") or "")

    @site_url.setter
    def site_url(self, value: str) -> None:
        self.data["site_url"] = value

    @property
    def cdp_port(self) -> int:
        try:
            return int(self.data.get("cdp_port") or 9222)
        except (TypeError, ValueError):
            return 9222

    def get_token(self) -> str:
        if keyring is not None:
            try:
                token = keyring.get_password(SERVICE, "api_token")
                if token:
                    return token
            except Exception:
                pass
        return str(self.data.get("api_token") or "")

    def set_token(self, token: str) -> None:
        token = token.strip()
        if keyring is not None:
            try:
                keyring.set_password(SERVICE, "api_token", token)
                self.data.pop("api_token", None)
                self.token_in_file = False
                return
            except Exception:
                pass
        self.data["api_token"] = token
        self.token_in_file = True

    def mark_sent(self, result_uid: str, info: dict) -> None:
        self.data.setdefault("sent", {})[result_uid] = info
        self.save()

    def sent_info(self, result_uid: str) -> Optional[dict]:
        return self.data.get("sent", {}).get(result_uid)
