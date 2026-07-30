"""Drive the MCP server over the protocol, not by calling the functions.

The claim this phase makes is "I wrote an MCP server", and calling
`registry.fetch_offers()` directly would not support it — that only proves a
Python function exists. These tests connect a real MCP client, perform the real
initialize handshake, list tools the way any client would, and call them over
JSON-RPC. The transport is in-memory; the protocol is not mocked.

If a schema is malformed, a tool is misregistered, or a result is not
serialisable, this fails where the unit tests would not.
"""

from __future__ import annotations

import json

import pytest

from app import db
from app.ingress import repository
from app.privacy.refs import customer_ref

pytestmark = pytest.mark.integration


def connect():
    """A connected client session.

    Entered inside each test rather than provided as a fixture: the client holds
    an anyio cancel scope, and pytest-asyncio finalises generator fixtures in a
    different task than it creates them, which anyio refuses.
    """
    from mcp import Client

    from app.tools.server import mcp

    return Client(mcp)


def payload(result) -> dict:
    """MCP returns content blocks; these tools return JSON objects inside them."""
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError(f"no usable content in {result}")


EXPECTED_TOOLS = {
    "check_eligibility",
    "fetch_offers",
    "verify_pan",
    "create_application",
    "schedule_followup",
    "escalate_to_human",
}


# ================================================================= discovery
async def test_a_client_can_list_the_tools(live_db) -> None:
    async with connect() as session:
        result = await session.list_tools()
    assert {t.name for t in result.tools} == EXPECTED_TOOLS


async def test_every_tool_advertises_a_description_and_a_schema(live_db) -> None:
    """A tool without a description is a tool no agent can choose correctly."""
    async with connect() as session:
        result = await session.list_tools()
    for tool in result.tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.input_schema, f"{tool.name} has no input schema"
        assert tool.input_schema.get("type") == "object"


async def test_the_schemas_are_narrow(live_db) -> None:
    """Tool design is the skill: no free-form catch-all arguments."""
    async with connect() as session:
        result = await session.list_tools()
    schemas = {t.name: t.input_schema for t in result.tools}

    required = set(schemas["create_application"].get("required") or [])
    assert {
        "conversation_id",
        "offer_id",
        "consent_ref",
        "idem_key",
    } <= required, "create_application must not be callable without consent and an idempotency key"

    for name, schema in schemas.items():
        props = set(schema.get("properties") or {})
        assert not props & {
            "kwargs",
            "query",
            "args",
            "params",
        }, f"{name} advertises a catch-all argument"


# ================================================================== calling
async def test_calling_a_read_tool_over_the_protocol(live_db) -> None:
    async with connect() as session:
        result = await session.call_tool(
            "check_eligibility",
            {
                "product": "personal_loan",
                "income_band": "above_1l",
                "city_tier": 1,
                "amount_inr": 500_000,
                "stage": "qualify",
            },
        )
    body = payload(result)
    assert body["eligible"] is True
    assert body["lender_count"] >= 1


async def test_the_guard_applies_over_the_protocol_too(live_db) -> None:
    """The important one: MCP is not a way around the stage rules.

    An external agent connecting to this server gets exactly the same refusal
    the in-process caller does, because both go through client.invoke().
    """
    ref = customer_ref("919000000066")
    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)
    conv = await repository.get_or_create_conversation("whatsapp", ref)
    cid = str(conv["id"])

    async with connect() as session:
        result = await session.call_tool(
            "create_application",
            {
                "conversation_id": cid,
                "offer_id": "off_anything",
                "consent_ref": "anything",
                "idem_key": "k",
                "stage": "close",
            },
        )
    body = payload(result)
    assert body["error"] == "tool_not_permitted"
    assert body["reason"] == "consent_missing"

    await db.execute("DELETE FROM conversations WHERE customer_ref = %s", ref)


async def test_a_stage_scoped_tool_is_refused_at_the_wrong_stage(live_db) -> None:
    async with connect() as session:
        result = await session.call_tool("verify_pan", {"pan": "ABCDE1234F", "stage": "qualify"})
    assert payload(result)["error"] == "tool_not_permitted"


async def test_verify_pan_over_the_protocol_never_echoes_the_number(live_db) -> None:
    async with connect() as session:
        result = await session.call_tool(
            "verify_pan", {"pan": "ABCDE1234F", "stage": "kyc_collect"}
        )
    body = payload(result)
    assert body["verified"] is True
    assert "ABCDE1234F" not in json.dumps(body)
