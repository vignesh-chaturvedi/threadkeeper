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

from app.obs import brand, queries

router = APIRouter(prefix="/console", tags=["console"])

# Stage colours, warm to cool down the funnel. Deliberately not red/green: the
# funnel narrowing is normal, not a failure, and colouring the last stage red
# would read as an alarm on every screenshot.
_STAGE_TINT = {
    "qualify": "#1cb0f6",
    "consent": "#4a9df4",
    "kyc_collect": "#7b8cf2",
    "offer_match": "#a87ae8",
    "close": "#ce82ff",
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
def _funnel_chart(rows: list[dict[str, Any]]) -> str:
    """A horizontal bar per stage, widths proportional to conversations reached.

    Proportional to the *total*, not to the widest bar. Normalising to the
    largest stage would make every funnel look identical no matter how steeply
    it drops, which is the one thing this chart exists to show.

    Laid out in CSS rather than SVG, which is a change from the first version.
    An SVG with a fixed viewBox scales its own text with the drawing, so the
    chart's type was larger than the page's on a wide window and smaller on a
    narrow one — a chart that would not share the typography of the document it
    sits in. It also had to hand-place the drop-off label in the 8px gap between
    bars, where a two-digit count overlapped the bar above. Both problems are
    layout problems, so they belong to the layout engine.
    """
    if not rows:
        return '<p class="empty">No conversations yet. Run the seeder.</p>'

    total = max(rows[0]["total"], 1)
    parts = ['<div class="funnel">']

    for row in rows:
        # A zero-width bar is drawn as nothing at all. The old chart floored the
        # width at 2px, which put a visible sliver against every empty stage and
        # made "nobody got here" look like "somebody just about did".
        pct = row["reached"] / total * 100
        tint = _STAGE_TINT.get(row["stage"], "#1cb0f6")

        if row["dropped_here"]:
            lost = 100 - (row["pct_of_previous"] or 0)
            parts.append(
                f'<div class="fdrop"><span>&minus;{row["dropped_here"]} '
                f"&middot; {lost:.0f}% lost here</span></div>"
            )

        parts.append(
            f'<div class="frow">'
            f'<div class="fl">{html.escape(row["stage"])}</div>'
            f'<div class="ftrack">'
            f'<div class="fbar" style="width:{pct:.2f}%;background:{tint}"></div>'
            f"</div>"
            f'<div class="fv"><b>{row["reached"]}</b>'
            f"<span>{row['pct_of_total']:.0f}%</span></div>"
            f"</div>"
        )

    parts.append("</div>")
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
      {_funnel_chart(rows)}
      <div class="tw"><table>
        <tr><th>stage</th><th>reached</th><th>of total</th><th>of prev</th>
            <th>lost here</th></tr>
        {funnel_rows}
      </table></div>
    </section>

    <section>
      <h2>Where they stop, and why</h2>
      <p class="sub">Last turn of each conversation, grouped by the exit condition that fired.
        "Leaves at KYC" and "leaves at KYC after an objection" are different problems.</p>
      <div class="tw"><table>
        <tr><th>stage</th><th>reason</th><th>intent</th><th>n</th></tr>
        {drop_rows}
      </table></div>
    </section>

    <section>
      <h2>Cost by stage</h2>
      <p class="sub">Spend attributed to the stage a turn entered. Latency is mean, not p50 —
        stated because at this volume a percentile would be theatre.</p>
      <div class="tw"><table>
        <tr><th>stage</th><th>turns</th><th>spend</th><th>mean latency</th><th>mean tokens</th></tr>
        {stage_rows}
      </table></div>
    </section>

    <section>
      <h2>Recent conversations</h2>
      <div class="tw"><table>
        <tr><th>id</th><th>stage</th><th>status</th><th>turns</th><th>cost</th><th>application</th></tr>
        {conv_rows}
      </table></div>
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
    <div class="cards five">
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
      <div class="tw"><table><tr><th>slot</th><th>value</th></tr>{slot_rows}</table></div>
    </section>
    """
    return HTMLResponse(_page(f"Conversation {str(header['id'])[:8]}", body))


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="icon" type="image/png" href="{brand.FAVICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800\
&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>{_CSS}</style></head>
<body>
<header class="topbar">
  <img src="{brand.MARK}" alt="" width="34" height="34">
  <span class="brand">Threadkeeper<small>console</small></span>
  <nav><a href="/console">Funnel</a><a href="/sim">Simulator</a></nav>
</header>
<div class="wrap">{body}
<footer>Every figure here is a query in <code>app/obs/queries.py</code></footer>
</div></body></html>"""


_CSS = """
/* Same system as /sim: Nunito, light paper, chunky borders, pressable edges.
   Two pages of one product should not look like two products. */
:root{
  --paper:#ffffff; --bg:#f2f5f7; --panel:#f7f9fa; --ink-deep:#06242c;
  --ink:#3c3c3c; --ink-2:#5a5a5a; --dim:#9aa0a6;
  --line:#e5e7eb; --line-2:#d4d8dd;
  --green:#58cc02; --green-d:#46a302; --blue:#1cb0f6; --blue-d:#1899d6;
  --gold:#ffc800; --gold-d:#a67c00; --red:#ff4b4b; --red-d:#c81e1e; --purple:#ce82ff;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:"Nunito",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
     font:400 15px/1.6 var(--sans);-webkit-font-smoothing:antialiased}
a{color:var(--blue-d);text-decoration:none;font-weight:700}
a:hover{text-decoration:underline}
code{font-family:var(--mono);font-size:.88em;color:var(--blue-d);
     background:rgba(28,176,246,.09);padding:1px 5px;border-radius:5px}

/* ---- top bar ---- */
.topbar{display:flex;align-items:center;gap:12px;padding:12px 22px;background:var(--ink-deep)}
.topbar img{flex-shrink:0}
.topbar .brand{font-weight:800;font-size:16px;color:#fff;line-height:1.15}
.topbar .brand small{display:block;font:500 9.5px/1 var(--mono);letter-spacing:.16em;
  text-transform:uppercase;color:rgba(255,255,255,.6);margin-top:3px}
.topbar nav{margin-left:auto;display:flex;gap:8px}
.topbar nav a{color:rgba(255,255,255,.92);font-weight:800;font-size:12px;
  letter-spacing:.03em;text-transform:uppercase;padding:7px 13px;border-radius:11px;
  border:2px solid rgba(255,255,255,.22);border-bottom-width:3px}
.topbar nav a:hover{background:rgba(255,255,255,.1);text-decoration:none}

.wrap{max-width:1000px;margin:0 auto;padding:34px 24px 90px}
h1{font-size:30px;font-weight:800;margin:0 0 22px;letter-spacing:-.5px}
h1 span{color:var(--dim);font-weight:700}
h2{font-size:19px;font-weight:800;margin:0 0 6px;letter-spacing:-.2px}
.back{margin:0 0 16px;font-size:13px}
section{margin-top:38px;background:var(--paper);border:2px solid var(--line);
        border-radius:18px;padding:22px 24px 26px}
.sub{color:var(--ink-2);font-size:13.5px;margin:0 0 18px;max-width:74ch}
.sub.note{margin:14px 0 0;padding:14px 16px;background:var(--paper);
          border:2px solid var(--line);border-radius:14px}

/* ---- stat cards ----
   A grid, not a flex row. Flex let each card size to its own content, so the
   two with wrapping labels ("per conversation", "per closed sale") pushed
   their values a line lower than the rest and the numbers stopped sharing a
   baseline. Equal columns plus a fixed label height fixes both. */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}
/* Card count decides the minimum: seven cards at 210px land 4+3, five at
   168px land in one row. auto-fit alone cannot know how many are coming, and
   a single minimum leaves one page or the other with an orphan. */
.cards.five{grid-template-columns:repeat(auto-fit,minmax(168px,1fr))}
.cards>div{background:var(--paper);border:2px solid var(--line);border-radius:14px;
           padding:13px 15px 15px;min-width:0}
.cards .k{font:500 9px/1.35 var(--mono);letter-spacing:.14em;text-transform:uppercase;
          color:var(--dim);min-height:2.7em}
.cards .v{font-size:21px;font-weight:800;margin-top:4px;letter-spacing:-.4px;
          white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ---- funnel ---- */
.funnel{margin:20px 0 6px}
.frow{display:grid;grid-template-columns:118px minmax(0,1fr) 88px;
      align-items:center;gap:14px;padding:5px 0}
.fl{font:500 12.5px var(--mono);color:var(--ink-2);text-align:right;
    overflow:hidden;text-overflow:ellipsis}
.ftrack{height:30px;background:var(--panel);border:2px solid var(--line);
        border-radius:9px;overflow:hidden}
.fbar{height:100%;border-radius:0;transition:width .3s ease}
.fv{font:500 12.5px var(--mono);color:var(--ink);white-space:nowrap}
.fv b{font-weight:700} .fv span{color:var(--dim);margin-left:7px}
/* The drop-off sits on its own line between bars, aligned to the track, rather
   than being squeezed into the gap where it used to collide with the bar above. */
.fdrop{display:grid;grid-template-columns:118px minmax(0,1fr) 88px;gap:14px;padding:1px 0}
.fdrop span{grid-column:2;font:500 10.5px var(--mono);color:var(--gold-d);
            background:rgba(255,200,0,.16);border-radius:6px;padding:2px 8px;justify-self:start}

/* ---- tables ---- */
/* Tables scroll inside their own box. 'Recent conversations' is six columns
   and does not fit a phone; unwrapped, it widened the whole document, which
   shifts the page margins and makes every other section look broken too. */
.tw{overflow-x:auto;margin-top:16px;-webkit-overflow-scrolling:touch}
table{width:100%;min-width:440px;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:9px 12px 9px 0;border-bottom:2px solid var(--line)}
th{font:500 9px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
td{color:var(--ink-2)} td:first-child{color:var(--ink);font-weight:700}
tr:last-child td{border-bottom:none}
.empty{color:var(--dim);font-style:italic;font-weight:400}

/* ---- pills ---- */
.pill{font:700 10px var(--mono);padding:3px 9px;border-radius:99px;
      border:2px solid var(--line-2);color:var(--ink-2);text-transform:uppercase}
.pill.escalated{color:var(--gold-d);border-color:rgba(255,200,0,.55);background:rgba(255,200,0,.13)}
.pill.opted_out{color:var(--red-d);border-color:rgba(255,75,75,.45);background:rgba(255,75,75,.09)}
.pill.active{color:var(--green-d);border-color:rgba(88,204,2,.45);background:rgba(88,204,2,.1)}
.pill.won{color:var(--green-d);border-color:rgba(88,204,2,.45);background:rgba(88,204,2,.1)}

/* ---- conversation replay ---- */
.turn{border:2px solid var(--line);border-radius:14px;background:var(--paper);
      margin-bottom:12px;overflow:hidden}
.turn-h{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 15px;
        border-bottom:2px solid var(--line);background:var(--panel);
        font:500 11px var(--mono)}
.turn-h .n{color:var(--dim)}
.turn-h .stages{color:var(--ink);font-weight:700}
.turn-h .arrow{color:var(--dim);padding:0 2px}
.turn-h .arrow.moved{color:var(--blue-d)}
.turn-h .reason{color:var(--dim)}
.turn-h .intent{color:var(--blue-d);background:rgba(28,176,246,.11);
                border-radius:6px;padding:2px 7px}
.turn-h .held{color:var(--gold-d)} .turn-h .degraded{color:var(--red-d)}
.msg{padding:11px 15px;font-size:14px;border-bottom:2px solid var(--line)}
.msg::before{font:500 9px var(--mono);letter-spacing:.14em;text-transform:uppercase;
             color:var(--dim);display:block;margin-bottom:5px}
.msg.cust{color:var(--ink);font-weight:600}
.msg.cust::before{content:"customer"}
.msg.agent{color:var(--ink-2)}
.msg.agent::before{content:"agent"}
.meta{padding:9px 15px;font:500 10.5px var(--mono);color:var(--dim);background:var(--panel)}
.tool{border:2px solid var(--line-2);border-radius:6px;padding:1px 6px;margin-left:7px;
      color:var(--blue-d)}
.tool.bad{color:var(--red-d);border-color:rgba(255,75,75,.45)}

footer{margin-top:44px;padding-top:20px;border-top:2px solid var(--line);
       font:500 11px var(--mono);color:var(--dim)}
@media (max-width:640px){
  .wrap{padding:24px 15px 60px}
  section{padding:18px 16px 20px;border-radius:14px}
  .frow,.fdrop{grid-template-columns:84px minmax(0,1fr) 70px;gap:9px}
  .fl,.fv{font-size:11px}
  .topbar{padding:10px 14px}
}
"""
