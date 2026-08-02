"""The console: one page that makes the system legible in ten seconds.

Thin on purpose, as the plan asks. No charting library, no build step, no
JavaScript framework — the funnel is an inline SVG generated from the same
numbers the JSON endpoints return, so the page works with scripting off and the
chart cannot disagree with the table beside it.

Three views:

    /console                    the funnel, unit economics, recent conversations
    /console/c/{id}             one conversation, replayed with stage annotations
    /console/api/*              the same numbers as JSON, for anything else

The JSON endpoints exist because a dashboard is a bad interface for a machine.
Phase 12's write-up needs these figures; so does anyone who wants to check them.
"""

from __future__ import annotations

import html
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.obs import queries

router = APIRouter(prefix="/console", tags=["console"])

# Stage colours, warm to cool down the funnel. Deliberately not red/green: the
# funnel narrowing is normal, not a failure, and colouring the last stage red
# would read as an alarm on every screenshot.
_STAGE_TINT = {
    "qualify": "#6d8bff",
    "consent": "#7d84f5",
    "kyc_collect": "#8d7ee2",
    "offer_match": "#a878cf",
    "close": "#c072b8",
}


def _usd(value: Decimal | float | None, places: int = 5) -> str:
    if value is None:
        return "—"
    return f"${Decimal(value):.{places}f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


# ---------------------------------------------------------------------------
# JSON — the numbers, unstyled
# ---------------------------------------------------------------------------
@router.get("/api/funnel")
async def api_funnel() -> dict[str, Any]:
    return {
        "funnel": await queries.funnel(),
        "drop_off": await queries.drop_off_reasons(),
    }


@router.get("/api/cost")
async def api_cost() -> dict[str, Any]:
    economics = await queries.unit_economics()
    return {
        **{k: (float(v) if isinstance(v, Decimal) else v) for k, v in economics.items()},
        "by_stage": [{**row, "usd": float(row["usd"])} for row in await queries.cost_by_stage()],
    }


@router.get("/api/conversations/{conversation_id}")
async def api_conversation(conversation_id: str) -> dict[str, Any]:
    header = await _header_or_404(conversation_id)
    return {
        "conversation": {**header, "id": str(header["id"])},
        "turns": [
            {**t, "conversation_id": str(t["conversation_id"]), "cost_usd": float(t["cost_usd"])}
            for t in await queries.conversation_trace(conversation_id)
        ],
        "transcript": await queries.transcript(conversation_id),
    }


async def _header_or_404(conversation_id: str) -> dict[str, Any]:
    try:
        uuid.UUID(conversation_id)
    except ValueError:
        # Postgres raises on a malformed uuid, which would surface as a 500 for
        # what is plainly a bad request.
        raise HTTPException(status_code=404, detail="no such conversation") from None
    header = await queries.conversation_header(conversation_id)
    if header is None:
        raise HTTPException(status_code=404, detail="no such conversation")
    return header


# ---------------------------------------------------------------------------
# The funnel chart, as SVG
# ---------------------------------------------------------------------------
def _funnel_svg(rows: list[dict[str, Any]]) -> str:
    """A horizontal bar per stage, widths proportional to conversations reached.

    Proportional to the *total*, not to the widest bar. Normalising to the
    largest stage would make every funnel look identical no matter how steeply
    it drops, which is the one thing this chart exists to show.
    """
    if not rows:
        return '<p class="empty">No conversations yet. Run the seeder.</p>'

    total = max(rows[0]["total"], 1)
    row_h, gap, label_w, bar_w = 34, 8, 104, 420
    height = len(rows) * (row_h + gap)
    parts = [
        f'<svg viewBox="0 0 {label_w + bar_w + 150} {height}" role="img" '
        f'aria-label="Funnel drop-off by stage" class="funnel">'
    ]

    for i, row in enumerate(rows):
        y = i * (row_h + gap)
        width = max(2, round(row["reached"] / total * bar_w))
        tint = _STAGE_TINT.get(row["stage"], "#6d8bff")
        parts.append(
            f'<text x="{label_w - 10}" y="{y + 21}" class="fl" text-anchor="end">'
            f"{html.escape(row['stage'])}</text>"
            f'<rect x="{label_w}" y="{y}" width="{bar_w}" height="{row_h}" class="ftrack"/>'
            f'<rect x="{label_w}" y="{y}" width="{width}" height="{row_h}" fill="{tint}"/>'
            f'<text x="{label_w + bar_w + 12}" y="{y + 21}" class="fv">'
            f"{row['reached']} · {row['pct_of_total']:.0f}%</text>"
        )
        # The step-conversion label sits in the gap between bars, where the drop
        # actually happens, rather than in a column to the side.
        if row["dropped_here"]:
            parts.append(
                f'<text x="{label_w + 6}" y="{y - 1}" class="fd">'
                f"-{row['dropped_here']} ({100 - (row['pct_of_previous'] or 0):.0f}% lost)</text>"
            )

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    rows = await queries.funnel()
    economics = await queries.unit_economics()
    by_stage = await queries.cost_by_stage()
    drops = await queries.drop_off_reasons()
    recent = await queries.recent_conversations()

    cards = "".join(
        f'<div><div class="k">{html.escape(k)}</div><div class="v">{v}</div></div>'
        for k, v in [
            ("conversations", economics["conversations"]),
            ("turns", economics["turns"]),
            ("closed sales", f"{economics['sales']} · {economics['conversion_pct']:.1f}%"),
            ("escalated", economics["escalated"]),
            ("total spend", _usd(economics["total_usd"], 4)),
            ("per conversation", _usd(economics["usd_per_conversation"])),
            ("per closed sale", _usd(economics["usd_per_sale"], 4)),
        ]
    )
    priced_note = (
        f"Money is measured over the {economics['priced_conversations']} conversations "
        f"({economics['priced_turns']} turns) that ran against a priced model. The other "
        f"{economics['conversations'] - economics['priced_conversations']} ran on the "
        f"<code>fake</code> provider, which makes no call and costs nothing — averaging "
        f"real spend across those would report a cheaper system rather than a real number."
    )

    funnel_rows = "".join(
        f"<tr><td>{html.escape(r['stage'])}</td><td>{r['reached']}</td>"
        f"<td>{_pct(r['pct_of_total'])}</td><td>{_pct(r['pct_of_previous'])}</td>"
        f"<td>{r['dropped_here'] if r['dropped_here'] is not None else '—'}</td></tr>"
        for r in rows
    )

    stage_rows = (
        "".join(
            f"<tr><td>{html.escape(r['stage'])}</td><td>{r['turns']}</td>"
            f"<td>{_usd(r['usd'], 5)}</td><td>{int(r['p50_ms'] or 0)} ms</td>"
            f"<td>{int(r['avg_tokens'] or 0)}</td></tr>"
            for r in by_stage
        )
        or '<tr><td colspan="5" class="empty">no turns recorded</td></tr>'
    )

    drop_rows = (
        "".join(
            f"<tr><td>{html.escape(r['stage'])}</td><td>{html.escape(r['reason'])}</td>"
            f"<td>{html.escape(r['intent'] or '—')}</td><td>{r['n']}</td></tr>"
            for r in drops
        )
        or '<tr><td colspan="4" class="empty">no turns recorded</td></tr>'
    )

    conv_rows = (
        "".join(
            f'<tr><td><a href="/console/c/{r["id"]}">{str(r["id"])[:8]}</a></td>'
            f"<td>{html.escape(r['stage'])}</td>"
            f'<td><span class="pill {html.escape(r["status"])}">'
            f"{html.escape(r['status'])}</span></td>"
            f"<td>{r['turns']}</td><td>{_usd(r['usd'])}</td>"
            f"<td>{'✓' if r['has_application'] else '—'}</td></tr>"
            for r in recent
        )
        or '<tr><td colspan="6" class="empty">no conversations yet</td></tr>'
    )

    body = f"""
    <h1>Threadkeeper <span>console</span></h1>
    <div class="cards">{cards}</div>
    <p class="sub note">{priced_note}</p>

    <section>
      <h2>Funnel</h2>
      <p class="sub">Conversations that ever reached each stage, as a share of all
        conversations. A lead that reached offers and then opted out still reached offers.</p>
      {_funnel_svg(rows)}
      <table>
        <tr><th>stage</th><th>reached</th><th>of total</th><th>of prev</th>
            <th>lost here</th></tr>
        {funnel_rows}
      </table>
    </section>

    <section>
      <h2>Where they stop, and why</h2>
      <p class="sub">Last turn of each conversation, grouped by the exit condition that fired.
        "Leaves at KYC" and "leaves at KYC after an objection" are different problems.</p>
      <table>
        <tr><th>stage</th><th>reason</th><th>intent</th><th>n</th></tr>
        {drop_rows}
      </table>
    </section>

    <section>
      <h2>Cost by stage</h2>
      <p class="sub">Spend attributed to the stage a turn entered. Latency is mean, not p50 —
        stated because at this volume a percentile would be theatre.</p>
      <table>
        <tr><th>stage</th><th>turns</th><th>spend</th><th>mean latency</th><th>mean tokens</th></tr>
        {stage_rows}
      </table>
    </section>

    <section>
      <h2>Recent conversations</h2>
      <table>
        <tr><th>id</th><th>stage</th><th>status</th><th>turns</th><th>cost</th><th>application</th></tr>
        {conv_rows}
      </table>
    </section>
    """
    return HTMLResponse(_page("Threadkeeper · console", body))


@router.get("/c/{conversation_id}", response_class=HTMLResponse)
async def inspector(conversation_id: str) -> HTMLResponse:
    header = await _header_or_404(conversation_id)
    turns = await queries.conversation_trace(conversation_id)
    messages = await queries.transcript(conversation_id)

    slots = header["slots"] or {}
    slot_rows = (
        "".join(
            f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(str(v))}</td></tr>"
            for k, v in sorted(slots.items())
        )
        or '<tr><td colspan="2" class="empty">nothing learned yet</td></tr>'
    )

    # The transcript and the traces are interleaved by ordinal rather than by
    # timestamp: one turn is one inbound message and one reply, and matching on
    # time would scramble the pairing whenever a burst was coalesced into a
    # single turn — which is exactly what Phase 02 does on purpose.
    inbound = [m for m in messages if m["direction"] == "in"]
    outbound = [m for m in messages if m["direction"] == "out"]

    blocks = []
    for i, turn in enumerate(turns):
        cust = inbound[i]["body"] if i < len(inbound) else None
        agent = outbound[i]["body"] if i < len(outbound) else None
        tools = turn["tools"] or []
        tool_html = "".join(
            '<span class="tool{}">{}{}</span>'.format(
                " bad" if (t.get("error") or t.get("denied_reason")) else "",
                html.escape(t["tool"]),
                f" · {t['latency_ms']}ms" if t.get("latency_ms") else "",
            )
            for t in tools
        )
        moved = turn["stage_in"] != turn["stage_out"]
        blocks.append(f"""
        <div class="turn">
          <div class="turn-h">
            <span class="n">turn {turn["turn_index"]}</span>
            <span class="stages">{html.escape(turn["stage_in"])}
              <span class="{"arrow moved" if moved else "arrow"}">→</span>
              {html.escape(turn["stage_out"])}</span>
            <span class="reason">{html.escape(turn["reason"])}</span>
            {f'<span class="intent">{html.escape(turn["intent"])}</span>' if turn["intent"] else ""}
            {'<span class="held">held</span>' if turn["held_stage"] else ""}
            {'<span class="degraded">degraded</span>' if turn["degraded"] else ""}
          </div>
          {f'<div class="msg cust">{html.escape(cust)}</div>' if cust else ""}
          {f'<div class="msg agent">{html.escape(agent)}</div>' if agent else ""}
          <div class="meta">
            {turn["tokens_in"]} in · {turn["tokens_out"]} out · {turn["model_calls"]} calls
            · {turn["latency_ms"]} ms · {_usd(turn["cost_usd"])}
            · ctx {turn["context_tokens"]}t
            · {html.escape(", ".join(turn["memory_tiers"]) or "no memory")}
            {f"<span class='tools'>{tool_html}</span>" if tool_html else ""}
          </div>
        </div>""")

    if not blocks:
        blocks = [
            '<p class="empty">No traced turns. Conversations from before Phase 10 '
            "have transcripts but no traces — the table did not exist when they ran.</p>"
        ]

    body = f"""
    <p class="back"><a href="/console">← console</a></p>
    <h1>Conversation <span>{str(header["id"])[:8]}</span></h1>
    <div class="cards">
      <div><div class="k">stage</div><div class="v">{html.escape(header["stage"])}</div></div>
      <div><div class="k">status</div><div class="v">{html.escape(header["status"])}</div></div>
      <div><div class="k">turns</div><div class="v">{len(turns)}</div></div>
      <div><div class="k">spend</div>
        <div class="v">{_usd(sum(t["cost_usd"] for t in turns), 5)}</div></div>
      <div><div class="k">applications</div><div class="v">{header["applications"]}</div></div>
    </div>

    <section>
      <h2>Replay</h2>
      <p class="sub">Every turn, annotated with the stage it entered, the stage it left, and the
        condition that decided the move.</p>
      {"".join(blocks)}
    </section>

    <section>
      <h2>What it learned</h2>
      <table><tr><th>slot</th><th>value</th></tr>{slot_rows}</table>
    </section>
    """
    return HTMLResponse(_page(f"Conversation {str(header['id'])[:8]}", body))


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style></head>
<body><div class="wrap">{body}
<footer>Threadkeeper · every figure here is a query in app/obs/queries.py</footer>
</div></body></html>"""


_CSS = """
:root{--ink:#0c0e13;--ink2:#10131a;--line:#1e2431;--text:#e7e5df;--dim:#6d7484;
      --text2:#a6acba;--accent:#9db4ff;--ok:#7fb98f;--warn:#e0a24a;--bad:#e07a7a;
      --mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--ink);color:var(--text);font:400 14px/1.6 -apple-system,
     BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto;padding:44px 28px 90px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:30px;font-weight:500;margin:0 0 26px;letter-spacing:-.4px}
h1 span{color:var(--dim);font-weight:300}
h2{font-size:17px;font-weight:500;margin:0 0 4px}
.back{margin:0 0 14px;font-size:12px}
section{margin-top:46px;border-top:1px solid var(--line);padding-top:22px}
.sub{color:var(--dim);font-size:12.5px;margin:0 0 18px;max-width:70ch}
.sub.note{margin-top:12px}
.sub code{font-family:var(--mono);color:var(--text2)}
.cards{display:flex;flex-wrap:wrap;gap:1px;background:var(--line);border:1px solid var(--line)}
.cards>div{background:var(--ink2);padding:12px 15px;flex:1 1 120px}
.cards .k{font:400 9px/1 var(--mono);letter-spacing:.15em;text-transform:uppercase;color:var(--dim)}
.cards .v{font-size:17px;margin-top:6px}
table{width:100%;border-collapse:collapse;margin-top:16px;font-size:13px}
th,td{text-align:left;padding:8px 12px 8px 0;border-bottom:1px solid var(--line)}
th{font:400 9px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
td{color:var(--text2)}td:first-child{color:var(--text)}
.empty{color:var(--dim);font-style:italic}
.funnel{width:100%;height:auto;margin:6px 0 4px;overflow:visible}
.funnel .ftrack{fill:#161a24}
.funnel .fl{font:400 12px var(--mono);fill:var(--text2)}
.funnel .fv{font:400 12px var(--mono);fill:var(--text)}
.funnel .fd{font:400 9.5px var(--mono);fill:var(--warn)}
.pill{font:400 10px var(--mono);padding:2px 7px;border:1px solid var(--line);color:var(--dim)}
.pill.escalated{color:var(--warn);border-color:rgba(224,162,74,.4)}
.pill.opted_out{color:var(--bad);border-color:rgba(224,122,122,.4)}
.pill.active{color:var(--ok);border-color:rgba(127,185,143,.35)}
.turn{border:1px solid var(--line);background:var(--ink2);margin-bottom:11px}
.turn-h{display:flex;flex-wrap:wrap;gap:11px;align-items:center;padding:9px 14px;
        border-bottom:1px solid var(--line);font:400 11px var(--mono)}
.turn-h .n{color:var(--dim)}
.turn-h .stages{color:var(--text)}
.turn-h .arrow{color:var(--dim);padding:0 2px}
.turn-h .arrow.moved{color:var(--accent)}
.turn-h .reason{color:var(--dim)}
.turn-h .intent{color:var(--accent);border:1px solid rgba(157,180,255,.3);padding:1px 6px}
.turn-h .held{color:var(--warn)}
.turn-h .degraded{color:var(--bad)}
.msg{padding:9px 14px;font-size:13.5px;border-bottom:1px solid var(--line)}
.msg.cust{color:var(--text)}
.msg.cust::before{content:"customer ";font:400 9px var(--mono);letter-spacing:.14em;
                  text-transform:uppercase;color:var(--dim);display:block;margin-bottom:4px}
.msg.agent{color:var(--text2)}
.msg.agent::before{content:"agent ";font:400 9px var(--mono);letter-spacing:.14em;
                   text-transform:uppercase;color:var(--dim);display:block;margin-bottom:4px}
.meta{padding:7px 14px;font:400 10.5px var(--mono);color:var(--dim)}
.tool{border:1px solid var(--line);padding:1px 6px;margin-left:7px;color:var(--accent)}
.tool.bad{color:var(--bad);border-color:rgba(224,122,122,.4)}
footer{margin-top:70px;padding-top:18px;border-top:1px solid var(--line);
       font:400 10.5px var(--mono);color:var(--dim)}
@media (max-width:640px){.wrap{padding:28px 16px 60px}}
"""
