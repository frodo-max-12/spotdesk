# Broker playbook — spot-market negotiation norms

Operational system-of-record for how brokers behave through the RFQ → quote → order
cycle. The agent reads this before drafting vendor mail and before reacting to vendor
replies. Empirical basis: live RFQ flow, anchored on a May-2026 DDR3 deal
(MT41K128M16JT-125AAT:K) where 8+ brokers quoted and every pattern below played out.

## 1. What brokers gate behind a confirmed order

Brokers withhold these until you commit to a PO. Asking pre-order gets a polite
"after order" wall — standard and protective, NOT a sign of no stock:

| Gated item | Why they protect it | When you get it |
|---|---|---|
| Stock label photo | Reveals date code / lot / seal — IDs their upstream source | After PO (or paid sample) |
| CoC / CoO / traceability docs | Effort + reveals source pedigree | After PO |
| Full reel / lot markings | Same source-exposure risk | After PO |

Don't push past the wall — you only look green. Use text tells instead (§5).

## 2. Broker stall / anchor tactics

- **"What's your target price?"** — an anchoring attempt; they want you to set the floor.
  With market data: "market is $X–Y, beat it or pass." Without: decline to anchor —
  "please quote your best firm."
- **"Is the price acceptable? / can we order?"** — a buying signal; they sense a real
  deal. Means stock exists and you have leverage. Hold with a warm non-commit if the end
  customer hasn't confirmed (§4).

## 3. The graduated-ask ladder

How much you may ask scales with how close a PO is. Mismatch burns goodwill:

| Ask | Goodwill cost | OK without PO? |
|---|---|---|
| Quote (price / DC / qty / lead) | Zero | Always — brokers blast these |
| Label photo / DC confirm | ~2 min | Usually gated, but asking signals intent |
| Hold stock 24–48h | Moderate | Only when PO is likely |
| Formal CoC/CoO, samples, allocation | High | Near-PO only |

A single non-converting RFQ costs ~nothing. The tire-kicker pattern is escalating asks
with zero conversions, repeated for months.

## 4. Relationship management

- **Non-conversion is normal** — spot quote-to-order runs ~5–20%. A dead RFQ is
  forgotten in days.
- **Close the loop** when a deal dies (also a cheap label-acquisition tool for the
  learning loop): "Thanks for your support on this — customer went another direction
  this time. Will keep you in mind for the next requirement."
- **Warm hold** while waiting on the end customer: "Price noted, thank you. Confirming
  with our customer — will revert shortly."

## 5. Reading broker tells

- "Asking the supplier for photos" / "let me check with supplier" → a reforwarding
  middleman, not the inventory holder; thin markup over an upstream source.
- "Price and availability subject to re-confirmation" → honest market-speak, not a
  hedge. Use the phrase yourself; respect it from others.
- Consistent DC week-code across "independent" brokers → same lot/source upstream.
  Same packaging + same lead time reinforce it.
- Tight price cluster (<10–15% spread across 3+ brokers) → co-integrated upstream;
  lowest quote ≈ source + one thin margin layer.

## 6. Negotiation posture (what the agent must enforce)

- **Never share the customer's target price with any vendor** — vendors only ever see a
  percentage ask against their OWN last cost, capped (one round's ask never exceeds the
  configured MAX_VENDOR_ASK_PERCENT).
- **Withhold our own target; collect independent quotes; leverage the lowest.**
- When a customer cites a competing price: acknowledge, "review and provide best revised
  price", "check allocation" — stall gracefully while re-sourcing.
- On no-stock: straight talk, then actively offer a cross-reference from the authorized
  line, anchoring long lead times to "allocation".

*Living doc — append new norms as they surface in live deals.*
