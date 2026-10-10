"""Prove that separate filenames cannot hide a shared Telegram login."""

from pathlib import Path
import sqlite3

import pytest

from telegram_translator.config_manager import ConfigManager
from telegram_translator.session_safety import assert_isolated_telegram_session


def _session(path: Path, key: bytes | None) -> None:
    """Create a hermetic Telethon-shaped authorization database."""
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sessions (auth_key BLOB)")
        connection.execute("INSERT INTO sessions VALUES (?)", (key,))


@pytest.fixture
def protected_dir(tmp_path, monkeypatch):
    """Keep the safety check away from real Telegram sessions."""
    directory = tmp_path / "tgp"
    directory.mkdir()
    monkeypatch.setenv("TGP_SESSIONS_DIR", str(directory))
    return directory


@pytest.mark.parametrize("suffix", ["", ".session"])
def test_copied_authorization_is_rejected_read_only(tmp_path, protected_dir, suffix):
    """Match keys despite different names and never alter the owner database."""
    original = protected_dir / "personal.session"
    copied = tmp_path / "translator.session"
    key = b"private-test-authorization" * 10
    _session(original, key)
    _session(copied, key)
    before = {path: path.read_bytes() for path in (original, copied)}

    with pytest.raises(ValueError, match="shares an authorization") as raised:
        assert_isolated_telegram_session(str(tmp_path / ("translator" + suffix)))

    assert "private-test-authorization" not in str(raised.value)
    assert before == {path: path.read_bytes() for path in before}


def test_independent_authorization_is_allowed(tmp_path, protected_dir):
    _session(protected_dir / "personal.session", b"owner-authorization")
    path = tmp_path / "translator.session"
    _session(path, b"independent-authorization")
    assert_isolated_telegram_session(str(path))


@pytest.mark.parametrize("exists", [False, True])
def test_fresh_session_can_be_authorized(tmp_path, protected_dir, exists):
    path = tmp_path / "fresh.session"
    if exists:
        _session(path, None)
    assert_isolated_telegram_session(str(path))
    assert path.exists() is exists


@pytest.mark.parametrize("exists", [False, True])
@pytest.mark.parametrize("symlink", [False, True])
def test_empty_or_missing_owner_path_cannot_be_authorized(
    tmp_path, protected_dir, exists, symlink
):
    """Reject path aliases before a fresh login can overwrite TGP's file."""
    owner_path = protected_dir / "personal.session"
    if exists:
        _session(owner_path, None)
    candidate = owner_path
    if symlink:
        candidate = tmp_path / "translator.session"
        candidate.symlink_to(owner_path)
    with pytest.raises(ValueError, match="path belongs to TGP"):
        assert_isolated_telegram_session(str(candidate))


@pytest.mark.parametrize("corrupt_protected", [False, True])
def test_unreadable_database_is_distinct_from_no_authorization(
    tmp_path, protected_dir, corrupt_protected
):
    path = tmp_path / "translator.session"
    _session(path, b"translator-authorization")
    corrupt = protected_dir / "personal.session" if corrupt_protected else path
    corrupt.write_bytes(b"invalid database")
    with pytest.raises(RuntimeError, match="Cannot validate"):
        assert_isolated_telegram_session(str(path))


def test_credentials_refuse_shared_login_before_client_construction(
    tmp_path, protected_dir, monkeypatch
):
    monkeypatch.setenv("TTR_API_ID", "123")
    monkeypatch.setenv("TTR_API_HASH", "test-api-hash")
    monkeypatch.setenv("TTR_SESSION_NAME", "translator")
    _session(protected_dir / "personal.session", b"shared-authorization")
    _session(tmp_path / "translator.session", b"shared-authorization")
    manager = ConfigManager.__new__(ConfigManager)
    manager.sessions_dir = tmp_path
    manager.config = {}

    with pytest.raises(ValueError, match="shares an authorization"):
        manager.get_telegram_credentials()
