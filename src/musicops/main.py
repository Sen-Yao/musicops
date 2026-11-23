from __future__ import annotations

import os
from pathlib import Path

from musicops.api import create_app
from musicops.library import LibraryInspector
from musicops.vmomo import TelethonVmomoClient, UnconfiguredVmomoClient


def _get_data_dir() -> Path:
    return Path(os.environ.get("MUSICOPS_DATA_DIR", "/data"))


def _get_api_token() -> str:
    token = os.environ.get("MUSICOPS_API_TOKEN")
    if token:
        return token
    token_file = os.environ.get("MUSICOPS_API_TOKEN_FILE")
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("MUSICOPS_API_TOKEN_FILE could not be read") from exc
        if token:
            return token
        raise RuntimeError("MUSICOPS_API_TOKEN_FILE is empty")
    raise RuntimeError("MUSICOPS_API_TOKEN or MUSICOPS_API_TOKEN_FILE is required")


def _get_library_root() -> Path:
    return Path(os.environ.get("MUSICOPS_LIBRARY_ROOT", "/music"))


def _get_vmomo_client(data_dir: Path):
    if not os.environ.get("TELEGRAM_API_ID") or not os.environ.get("TELEGRAM_API_HASH"):
        return UnconfiguredVmomoClient()
    session_name = os.environ.get("MUSICOPS_TELEGRAM_SESSION_NAME", "senyao_userbot")
    try:
        return TelethonVmomoClient.from_env(data_dir, session_name=session_name)
    except (OSError, RuntimeError, ValueError) as exc:
        return UnconfiguredVmomoClient(str(exc))


_data_dir = _get_data_dir()
app = create_app(
    data_dir=_data_dir,
    api_token=_get_api_token(),
    library_inspector=LibraryInspector(_get_library_root()),
    vmomo_client=_get_vmomo_client(_data_dir),
)
