"""Start password and phone PIN (stdlib-only).

* Start password: the hub only starts after it is entered on the laptop (`ahub on`).
  It stops the hub from being started by accident, by a service, or by another program.
* Phone PIN: phone commands that act (run scripts, start or message agents, answer
  permission prompts, fetch files) need `/unlock <PIN>` first. It protects you if
  someone gets into your Telegram account. It is set only on the laptop (`ahub pin`).

Only salted PBKDF2 hashes are stored (<hub home>/password, <hub home>/phone_pin).
Neither protects against someone who already controls your user account: they could
edit this code. Lock your laptop.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from .config import hub_home, write_private

ITERATIONS = 600_000


def _path():
    return hub_home() / "password"


def _pin_path():
    return hub_home() / "phone_pin"


def _hash(secret: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, ITERATIONS)


def _store(path, secret: str) -> None:
    salt = secrets.token_bytes(16)
    write_private(path, f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${_hash(secret, salt).hex()}\n")


def _check(path, secret: str) -> bool:
    try:
        _, iterations, salt, digest = path.read_text().strip().split("$")
    except (OSError, ValueError):
        return False
    got = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(got.hex(), digest)


def has_password() -> bool:
    return _path().exists()


def set_password(password: str) -> None:
    _store(_path(), password)


def check_password(password: str) -> bool:
    return _check(_path(), password)


def has_pin() -> bool:
    return _pin_path().exists()


def set_pin(pin: str) -> None:
    _store(_pin_path(), pin)


def check_pin(pin: str) -> bool:
    return _check(_pin_path(), pin)
