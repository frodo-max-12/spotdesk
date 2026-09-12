# Outreach playbook — cold email + follow-up craft

Validated across 7,000+ real sends (11 batches) from this desk. The campaign engine
enforces the hard rules in code; this file carries the craft the drafter reads.

## The cold email (locked format)

- **HTML, minimal table with 1px borders. No images. No attachments** (PDFs trigger
  spam and violate the anti-forwarding rule).
- **Subject — specific part-number anchor, plain hyphen:**
  `Samsung DDR5 64GB RDIMM (M321R8GA0EB2) - Spot Stock Available`
  `Infineon Server VRM (TDA22590) & Samsung DDR5 - Spot Stock`
  Never an em-dash. No date tags (newsletter-y).
- **Body order:** `Hi,` (no name — avoids mismatch risk, matches broker norm) → one
  warm line ("I'm glad to share today's hot offer to you. Hope this will be helpful:")
  → inline table `Part Number | MFG | Family | DC | Qty` → one-line CTA ("If any of
  these match your requirements, reply and I'll share latest pricing.") → signature.
- **Signature is locked in config** — no personal credentials or embellishments
  (tested; dropped as try-hard).
- Include an opt-out line ("If you'd prefer not to receive these, just reply and we'll
  remove you.") — replies feed the suppression list.

## Anti-forwarding SKU subsets (why the offer pool exists)

Broker offer emails get forwarded around the industry. If three brokers forward the
same SKU list, buyers realize everyone is selling the same source's inventory and
credibility dies. Therefore:

1. **5 SKUs per company** — never the full catalogue.
2. **Different SKU combinations across companies** — no two companies receive the same
   5. Companies of the same type draw from the same pool but get unique permutations.
3. **Quantity jitter ±20–30%** from the source sheet, rounded to ugly numbers
   (220, 880, 10,500 — not 250, 1000, 10000).
4. **Same quantities within a company** across its recipients (they share internal
   BOMs; asymmetric quantities look wrong).
5. **Never forward the source PDF.** Subset + rewrite inline, always.

## Pacing and volume (enforced by the engine)

- **Cold sends: 15–30s random jitter** between sends. Fixed intervals are detectable
  automation; sub-second bursts from newer senders get flagged.
- **Threaded follow-ups: 1s flat is safe** — replies on owned threads are high-trust
  (validated: 621 threaded sends, 0 failures). Never apply the 1s rule to cold.
- **Volume ramp by account history:** <1,000 lifetime cold sends → max 300/day;
  1,000–5,000 → 500/day; 5,000+ → 1,500/day. Workspace hard limit ~2,000/24h.
- **Bounce circuit-breaker:** rolling bounce rate above threshold → halt cold sends,
  verified-addresses only until it clears.
- **Domain blocklist:** domains with server-level rejects are hard-skipped; any domain
  accumulating 5+ hard bounces is auto-added.

## Follow-up cadence

| Days since last touch | Approach |
|---|---|
| 0–60 | **Threaded reply** on the original thread (same subject — Gmail threads on it) |
| 60–90 | Threaded OR fresh-subject re-engagement (judgment) |
| 90+ | Fresh subject + new hook — the old thread is buried |

Threaded bump body: "Spot stock update for this week - see below:" + a NEW 5-SKU combo
(rotate via the offer pool; same combo across one company) + "Reply with what's open
and I'll send pricing today." Weekly cadence: be in their inbox once per week.

Always exclude from follow-ups: bounced addresses, anyone who already replied,
blocked domains, suppression-listed contacts.

## Non-negotiables (enforced in code, listed here for humans)

1. **Test-first:** every campaign must send one rendered proof to the operator's own
   inbox before any bulk send is allowed.
2. **Per-campaign ledger:** re-running a campaign never double-sends; safe to stop and
   resume across days.
3. **Suppression is permanent** unless a human removes the entry.
4. One recipient per email — nobody ever sees another address.
