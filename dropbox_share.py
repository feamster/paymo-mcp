"""Dropbox share-link helper used by paymo-mcp invoice tooling.

Reads OAuth credentials (app_key, app_secret, refresh_token) from
`~/.mcp-auth/dropbox/auth.json` and mints share links for files inside the
user's Dropbox root. Idempotent: if a link already exists for a file, it is
reused rather than recreated.

The refresh-token flow is used so a single one-time browser authorization
lasts indefinitely (short-lived `sl.*` tokens are auto-renewed by the SDK).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List

import dropbox
from dropbox.exceptions import ApiError
from dropbox.sharing import RequestedVisibility, SharedLinkSettings


DROPBOX_ROOT = Path.home() / "Dropbox"
AUTH_PATH = Path.home() / ".mcp-auth" / "dropbox" / "auth.json"


class DropboxAuthError(RuntimeError):
    """Raised when Dropbox credentials are missing or malformed."""


def _load_creds() -> Dict[str, str]:
    if not AUTH_PATH.exists():
        raise DropboxAuthError(
            f"Dropbox auth not found at {AUTH_PATH}. "
            f"Expected keys: app_key, app_secret, refresh_token."
        )
    with open(AUTH_PATH) as f:
        creds = json.load(f)
    missing = [k for k in ("app_key", "app_secret", "refresh_token") if not creds.get(k)]
    if missing:
        raise DropboxAuthError(f"Dropbox auth missing keys: {missing}")
    return creds


def _client() -> dropbox.Dropbox:
    creds = _load_creds()
    dbx = dropbox.Dropbox(
        oauth2_refresh_token=creds["refresh_token"],
        app_key=creds["app_key"],
        app_secret=creds["app_secret"],
    )
    dbx.users_get_current_account()  # eager credential check
    return dbx


def local_to_dropbox_path(local: str | Path) -> str:
    """Convert /Users/<user>/Dropbox/<rest> to /<rest> (Dropbox API path)."""
    p = Path(local).expanduser().resolve()
    try:
        rel = p.relative_to(DROPBOX_ROOT.resolve())
    except ValueError as e:
        raise ValueError(
            f"Path {p} is not inside {DROPBOX_ROOT}"
        ) from e
    return "/" + str(rel).replace("\\", "/")


def get_share_link(dbx: dropbox.Dropbox, dbx_path: str) -> str:
    """Return an existing shared link for the file, creating one if needed."""
    existing = dbx.sharing_list_shared_links(path=dbx_path, direct_only=True).links
    if existing:
        return existing[0].url
    try:
        link = dbx.sharing_create_shared_link_with_settings(
            path=dbx_path,
            settings=SharedLinkSettings(requested_visibility=RequestedVisibility.public),
        )
    except ApiError:
        # some Dropbox account types reject the visibility setting
        link = dbx.sharing_create_shared_link_with_settings(path=dbx_path)
    return link.url


def get_share_links(local_paths: Iterable[str | Path]) -> Dict[str, str]:
    """Return {local_path_str: share_url} for each input path."""
    dbx = _client()
    out: Dict[str, str] = {}
    for local in local_paths:
        dbx_path = local_to_dropbox_path(local)
        out[str(local)] = get_share_link(dbx, dbx_path)
    return out
