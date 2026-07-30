"""Ingress logic that needs no database: signature checking and payload parsing."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.ingress.adapters.whatsapp import WhatsAppAdapter
from app.privacy.refs import customer_ref, normalise_msisdn
from app.settings import get_settings

SECRET = "test-app-secret"


def _sign(body: bytes, secret: str = SECRET) -> dict[str, str]:
    mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"x-hub-signature-256": f"sha256={mac}"}


def _payload(text: str = "hi", msg_id: str = "wamid.1", sender: str = "919876543210") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": sender,
                                    "id": msg_id,
                                    "timestamp": "1690000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }


# ------------------------------------------------------------------ signature
class TestSignature:
    def test_accepts_a_correct_hmac(self) -> None:
        adapter = WhatsAppAdapter()
        body = json.dumps(_payload()).encode()
        assert adapter.verify_signature(body, _sign(body)) is True

    def test_rejects_a_wrong_secret(self) -> None:
        adapter = WhatsAppAdapter()
        body = json.dumps(_payload()).encode()
        assert adapter.verify_signature(body, _sign(body, "not-the-secret")) is False

    def test_rejects_a_tampered_body(self) -> None:
        """The signature covers the bytes, so any edit invalidates it."""
        adapter = WhatsAppAdapter()
        body = json.dumps(_payload()).encode()
        headers = _sign(body)
        tampered = json.dumps(_payload(text="transfer me 10 lakh")).encode()
        assert adapter.verify_signature(tampered, headers) is False

    def test_rejects_a_missing_header(self) -> None:
        adapter = WhatsAppAdapter()
        assert adapter.verify_signature(b"{}", {}) is False

    def test_rejects_an_unprefixed_digest(self) -> None:
        adapter = WhatsAppAdapter()
        body = b"{}"
        mac = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert adapter.verify_signature(body, {"x-hub-signature-256": mac}) is False

    def test_header_lookup_is_case_insensitive(self) -> None:
        """Starlette lowercases, but a raw dict from a test or another channel may not."""
        adapter = WhatsAppAdapter()
        body = b"{}"
        mac = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert adapter.verify_signature(body, {"X-Hub-Signature-256": f"sha256={mac}"}) is True


# ---------------------------------------------------------------------- parse
class TestParse:
    def test_parses_a_text_message(self) -> None:
        events = WhatsAppAdapter().parse(_payload(text="need 5 lakh loan"))
        assert len(events) == 1
        evt = events[0]
        assert evt.text == "need 5 lakh loan"
        assert evt.provider_msg_id == "wamid.1"
        assert evt.channel == "whatsapp"
        assert evt.kind == "text"

    def test_status_callbacks_yield_no_events(self) -> None:
        """Delivery receipts hit the same endpoint. Zero events is success, not failure."""
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {"statuses": [{"id": "wamid.1", "status": "delivered"}]},
                        }
                    ]
                }
            ]
        }
        assert WhatsAppAdapter().parse(payload) == []

    def test_empty_and_junk_payloads_do_not_raise(self) -> None:
        adapter = WhatsAppAdapter()
        assert adapter.parse({}) == []
        assert adapter.parse({"entry": []}) == []
        assert adapter.parse({"entry": [{"changes": []}]}) == []

    def test_batched_messages_all_parse(self) -> None:
        """Meta batches; a webhook body can carry several messages at once."""
        payload = _payload()
        payload["entry"][0]["changes"][0]["value"]["messages"].append(
            {"from": "919876543210", "id": "wamid.2", "type": "text", "text": {"body": "second"}}
        )
        events = WhatsAppAdapter().parse(payload)
        assert [e.provider_msg_id for e in events] == ["wamid.1", "wamid.2"]

    def test_unsupported_types_are_flagged_not_dropped(self) -> None:
        """Silently ignoring a voice note looks like the bot ignoring the customer."""
        payload = _payload()
        payload["entry"][0]["changes"][0]["value"]["messages"][0] = {
            "from": "919876543210",
            "id": "wamid.audio",
            "type": "audio",
            "audio": {"id": "media-1"},
        }
        events = WhatsAppAdapter().parse(payload)
        assert len(events) == 1
        assert events[0].kind == "unsupported"

    def test_the_raw_phone_number_never_becomes_the_key(self) -> None:
        """A database dump must not be a directory of who was contacted."""
        events = WhatsAppAdapter().parse(_payload(sender="919876543210"))
        ref = events[0].customer_ref
        assert "9876543210" not in ref
        assert len(ref) == 32


# ----------------------------------------------------------------------- refs
class TestCustomerRef:
    @pytest.mark.parametrize(
        "raw",
        ["919876543210", "+91 98765-43210", "09876543210", "9876543210", "+919876543210"],
    )
    def test_all_formats_of_one_number_agree(self, raw: str) -> None:
        """Otherwise the same person gets a new conversation per formatting quirk."""
        assert customer_ref(raw) == customer_ref("919876543210")

    def test_different_numbers_differ(self) -> None:
        assert customer_ref("919876543210") != customer_ref("919876543211")

    def test_normalisation_adds_the_country_code(self) -> None:
        assert normalise_msisdn("9876543210") == "919876543210"


def test_production_config_refuses_to_boot_without_a_secret() -> None:
    """Fail at startup, not at 3am."""
    from pydantic import ValidationError

    from app.settings import Settings

    with pytest.raises(ValidationError, match="TK_WHATSAPP_APP_SECRET"):
        Settings(_env_file=None, env="prod", whatsapp_app_secret="")

    with pytest.raises(ValidationError, match="TK_CUSTOMER_REF_SECRET"):
        Settings(
            _env_file=None,
            env="prod",
            whatsapp_app_secret="real",
            customer_ref_secret="dev-only-customer-ref-secret",
        )


def test_simulator_is_forced_off_in_production() -> None:
    from app.settings import Settings

    s = Settings(
        _env_file=None,
        env="prod",
        whatsapp_app_secret="real",
        customer_ref_secret="real",
        enable_simulator=True,
    )
    assert s.enable_simulator is False


def test_signature_checking_is_on_whenever_a_secret_exists() -> None:
    assert get_settings().verify_signatures is True
