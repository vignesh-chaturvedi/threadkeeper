"""Customer references.

A phone number never becomes a primary key in this system. Conversations are
keyed by an HMAC of the number, so a database dump contains no directory of who
was contacted. The full tokenization vault — reversible, encrypted, covering PAN
and Aadhaar inside message bodies — is Phase 07; this is only the identifier.

Deterministic on purpose: the same number must resolve to the same conversation
across restarts and across nine days of silence.
"""

from __future__ import annotations

import hmac
import re
from hashlib import sha256

from app.settings import get_settings

_NON_DIGITS = re.compile(r"\D")


def normalise_msisdn(raw: str) -> str:
    """Strip formatting so +91 98765-43210, 919876543210 and 09876543210 agree.

    Indian numbers are 10 digits with a 91 country code; WhatsApp delivers them
    already E.164-ish, but the simulator and tests are looser than that.
    """
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) == 10:
        digits = "91" + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = "91" + digits[1:]
    return digits


def customer_ref(raw_msisdn: str) -> str:
    """Stable, non-reversible handle for a customer on a channel."""
    settings = get_settings()
    normalised = normalise_msisdn(raw_msisdn)
    digest = hmac.new(
        settings.customer_ref_secret.encode("utf-8"),
        normalised.encode("utf-8"),
        sha256,
    ).hexdigest()
    return digest[:32]
