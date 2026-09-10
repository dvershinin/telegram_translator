"""Private podcast credential loading and URL protection."""

import os
import re
import stat
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,}")
_MAX_CREDENTIAL_BYTES = 4096
_UNSAFE_CREDENTIAL = "Private podcast credential file is unavailable or unsafe"


def read_private_feed_token(token_file: str) -> str:
    """Read a private-feed token from a strictly protected local file.

    Args:
        token_file: Absolute owner-local credential file path.

    Returns:
        Validated URL-safe access token.

    Raises:
        RuntimeError: If the path, permissions, ownership, link count, or
            token contents are unsafe.
    """
    path = Path(token_file)
    try:
        if not path.is_absolute():
            raise ValueError("relative credential path")
        directory = path.parent.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
        ):
            raise ValueError("unsafe credential directory")

        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as credential:
            metadata = os.fstat(credential.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
            ):
                raise ValueError("unsafe credential file")
            raw = credential.read(_MAX_CREDENTIAL_BYTES + 1)

        if len(raw) > _MAX_CREDENTIAL_BYTES:
            raise ValueError("oversized credential")
        token = raw.decode("ascii").strip()
        if not _TOKEN_PATTERN.fullmatch(token):
            raise ValueError("invalid credential contents")
        return token
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise RuntimeError(_UNSAFE_CREDENTIAL) from error


def add_private_feed_token(url: str, token: str) -> str:
    """Add the private-feed token query parameter to a generated URL.

    Args:
        url: Generated podcast URL.
        token: Validated private-feed token.

    Returns:
        URL carrying the ``token`` query parameter.
    """
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("token", token))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )
