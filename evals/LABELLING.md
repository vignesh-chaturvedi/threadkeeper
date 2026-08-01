# Labelling guide — `intent_set.jsonl`

150 hand-labelled messages: 66 romanised Hinglish, 45 Devanagari, 39 mixed-script.
Every one is invented; none came from a real customer.

The point of writing this down is that a labelled set only stays useful if a
second person can extend it and get the same answers. An inconsistent gold set
does not measure a model, it measures the labeller's mood — and this one already
had five such rows before the rules below were made explicit.

## Format

```json
{"id": 142, "script": "latin", "text": "...", "intent": "product_enquiry",
 "slots": {"product": "personal_loan", "amount_inr": 500000}}
```

`script` is one of `latin` (romanised Hindi or English), `devanagari`, or
`mixed` (both inside one message). Results are reported per script, because
"handles Devanagari" is a claim that needs a number.

## Intent — exactly one per message

The question a label answers: **what is this message doing that would change
what the agent says next?** A label that changes nothing is a label nobody can
act on, which is why the taxonomy is twelve and not forty.

| intent | when |
|---|---|
| `greeting` | opens the conversation, asks for nothing |
| `product_enquiry` | names a loan type, or asks whether loans are available |
| `amount_request` | states an amount, **no product named** |
| `income_statement` | states income or salary |
| `kyc_status` | says whether they have a PAN |
| `consent_response` | answers the consent question, either way |
| `objection` | pushes back on price — rate, fees, EMI |
| `opt_out` | asks to stop being contacted |
| `escalation_request` | asks for a human, a manager, or to complain |
| `status_check` | asks what happened to an existing application |
| `off_topic` | conversational, unrelated to lending |
| `unclear` | too little to act on: "ok", "hmm", "ठीक" |

### Precedence, when a message does several things

Applied in this order. These exist because real messages rarely do one thing.

1. **`opt_out` beats everything.** "interest rate zyada hai isliye nahi chahiye,
   band karo" is an opt-out that happens to carry an objection. The objection is
   still recorded as a *slot*; the intent is the opt-out, because that is what
   changes the next action.
2. **`escalation_request` beats the funnel.** Asking for a human outranks
   whatever else was said.
3. **Naming a product makes it `product_enquiry`**, whatever else it carries.
   "10 lakh ka personal loan" is a product enquiry with an amount, not an amount
   request. An amount with no product named is `amount_request`.
4. **`objection` beats a bare question.** "EMI kitni banegi" is a price
   pushback, not a status check.
5. Otherwise the most specific fact stated wins: PAN → `kyc_status`, income →
   `income_statement`.

## Slots — only what the message actually says

Never inferred. "मुझे दो लाख का लोन चाहिए" gets `amount_inr` and no `product`,
because "लोन" alone does not name one. A slot the labeller had to guess at is a
slot the model will be marked wrong for not guessing identically.

| slot | values |
|---|---|
| `product` | `personal_loan` `home_loan` `business_loan` `gold_loan` |
| `amount_inr` | integer rupees — "5 lakh" is `500000`, "पचास हज़ार" is `50000` |
| `income_band` | `under_25k` `25k_50k` `50k_1l` `above_1l` |
| `pan_status` | `available` `missing` |
| `consent_granted` | `true` `false` |
| `opted_out` | `true` |
| `objection` | short label: `interest_rate` `fees` `emi` |

`objection` is scored fuzzily — "interest_rate" and "rate" mean the same thing,
and scoring them as different would measure vocabulary rather than
understanding. Every other slot is scored on exact equality.

## Deliberate traps

A set with no hard cases reports a flattering number that predicts nothing.

- **id 148** — "5 lakh se zyada salary hai meri". A lakh that is *income*, not an
  amount being borrowed. Only the keyword disambiguates, and an extractor that
  gets this wrong routes a real person to the wrong lender.
- **id 147** — "order 1234 5678 9012 ka status batao". Twelve digits that are
  not an Aadhaar; the checksum is what rejects it.
- **id 145 / 146** — an opt-out carrying an objection. Both must be recorded,
  and the intent must be the opt-out.
- **id 118 / 143 / 150** — script switching mid-sentence, including a number and
  a fact on opposite sides of the switch.
- **ids 58–60, 103–105, 140–141** — "ok", "ठीक", "hmm". Agreement to nothing in
  particular, which must not be read as consent.
