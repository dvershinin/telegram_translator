"""Refuse copied Telegram authorizations before opening a network client."""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3


def _authorization_fingerprints(path: Path) -> set[bytes]:
    """Read authorization fingerprints without changing a session database."""
    if not path.exists():
        return set()
    try:
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            rows = connection.execute("SELECT auth_key FROM sessions").fetchall()
    except sqlite3.Error as error:
        raise RuntimeError(
            f"Cannot validate Telegram session isolation: {path}"
        ) from error
    return {hashlib.sha256(key).digest() for (key,) in rows if key}


def assert_isolated_telegram_session(session_name: str) -> None:
    """Reject authorization keys also stored in TGP's session directory.

    Different SQLite filenames do not create independent Telegram logins.
    Connecting a copied authorization with another API layer changes the
    server's schema for the running owner and breaks its response decoding.

    Args:
        session_name: Telethon session filename, with or without its suffix.

    Raises:
        ValueError: The session shares a Telegram authorization with TGP.
        RuntimeError: A session database cannot be checked safely.
        OSError: The protected session directory cannot be inspected.
    """
    path = Path(session_name)
    if not str(path).endswith(".session"):
        path = Path(str(path) + ".session")
    protected_dir = Path(
        os.environ.get("TGP_SESSIONS_DIR", Path.home() / "Projects/tgp/sessions")
    )
    if path.resolve().is_relative_to(protected_dir.resolve()):
        raise ValueError(
            "Telegram session path belongs to TGP. Authorize a fresh login "
            "in the translator's own session directory."
        )
    fingerprints = _authorization_fingerprints(path)
    if not fingerprints:
        return
    if not protected_dir.exists():
        return
    for protected_path in protected_dir.iterdir():
        if protected_path.suffix != ".session":
            continue
        if fingerprints & _authorization_fingerprints(protected_path):
            raise ValueError(
                "Telegram session shares an authorization with TGP. "
                "Create a fresh Telegram login for the translator; copying or "
                "renaming a session file does not isolate it."
            )
