"""The token vault: encrypted at rest, one narrow way back out.

Two properties, and the second is the one people skip:

  * **Encrypted.** Fernet — AES-128-CBC with an HMAC, authenticated, so a
    tampered ciphertext fails loudly rather than decrypting to garbage.
  * **Deterministic tokens.** The same PAN in the same conversation always maps
    to the same token. Random tokens would give the model two different handles
    for one entity, and it would reason about them as two different PANs.

Determinism comes from an HMAC of the value, not from the ciphertext — Fernet
is deliberately non-deterministic (random IV per encryption), which is correct
for confidentiality and useless as a key.

Scoped per conversation: the same PAN in two conversations gets two tokens, so
the vault cannot be used to link customers by identifier.
"""

from __future__ import annotations

import hmac
from hashlib import sha256
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app import db
from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)

_cipher: Fernet | None = None


def cipher() -> Fernet:
    global _cipher
    if _cipher is None:
        _cipher = Fernet(get_settings().vault_key.encode())
    return _cipher


def reset_cipher() -> None:
    """Tests rotate the key; the cached cipher has to follow."""
    global _cipher
    _cipher = None


def token_for(conversation_id: str, kind: str, value: str) -> str:
    """Deterministic per (conversation, kind, value). Never reversible by itself."""
    material = f"{conversation_id}|{kind}|{value.strip().upper()}".encode()
    digest = hmac.new(get_settings().vault_key.encode(), material, sha256).hexdigest()
    return f"[{kind}_{digest[:10]}]"


async def put(conversation_id: str, kind: str, value: str) -> str:
    """Store a value and return its token. Idempotent."""
    token = token_for(conversation_id, kind, value)
    await db.execute(
        """
        INSERT INTO pii_vault (token, conversation_id, kind, ciphertext)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (token) DO NOTHING
        """,
        token,
        conversation_id,
        kind,
        cipher().encrypt(value.encode()).decode(),
    )
    return token


async def get(token: str) -> str | None:
    """The only way back. Every caller of this is a decision worth reviewing."""
    row = await db.fetch_one("SELECT ciphertext FROM pii_vault WHERE token = %s", token)
    if row is None:
        return None
    try:
        return cipher().decrypt(row["ciphertext"].encode()).decode()
    except InvalidToken:
        # Wrong key, or the ciphertext was altered. Either way, refuse.
        log.error("vault_decrypt_failed", token=token)
        return None


async def get_many(tokens: list[str]) -> dict[str, str]:
    if not tokens:
        return {}
    rows = await db.fetch_all(
        "SELECT token, ciphertext FROM pii_vault WHERE token = ANY(%s)", tokens
    )
    out: dict[str, str] = {}
    for row in rows:
        try:
            out[row["token"]] = cipher().decrypt(row["ciphertext"].encode()).decode()
        except InvalidToken:
            log.error("vault_decrypt_failed", token=row["token"])
    return out


async def forget(conversation_id: str) -> int:
    """Erase every stored identifier for one customer.

    The DPDP Act gives a right to erasure. Because messages, slots and logs all
    hold tokens rather than values, deleting these rows makes the identifiers
    unrecoverable everywhere at once — the tokens become permanently dangling,
    which is exactly what should happen.
    """
    deleted = await db.execute("DELETE FROM pii_vault WHERE conversation_id = %s", conversation_id)
    log.info("vault_erased", conversation_id=conversation_id, tokens=deleted)
    return deleted


async def inventory(conversation_id: str) -> list[dict[str, Any]]:
    """What we hold, without holding it up to the light."""
    rows = await db.fetch_all(
        "SELECT token, kind, created_at FROM pii_vault WHERE conversation_id = %s ORDER BY id",
        conversation_id,
    )
    return [
        {"token": r["token"], "kind": r["kind"], "at": r["created_at"].isoformat()} for r in rows
    ]
