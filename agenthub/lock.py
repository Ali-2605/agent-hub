"""Start password for the hub (stdlib-only).

The hub only starts after the password is entered on the laptop (`ahub on`).
Only a salted PBKDF2 hash is stored, in <hub home>/password.

This stops the hub from being started by accident, by a service, or by another
program. It does not protect against someone who already controls your user
account: they could edit this code. Lock your laptop.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from .config import hub_home

ITERATIONS = 600_000


def _path():
    return hub_home() / "password"


def has_password() -> bool:
    return _path().exists()


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)


def set_password(password: str) -> None:
    salt = secrets.token_bytes(16)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${_hash(password, salt).hex()}\n")
    os.chmod(p, 0o600)


def check_password(password: str) -> bool:
    try:
        _, iterations, salt, digest = _path().read_text().strip().split("$")
    except (OSError, ValueError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(got.hex(), digest)
