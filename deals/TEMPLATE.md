---
deal_id: <QUOTE-NUMBER>
customer_company:
customer_email:
opened:
closed:
status: in_progress   # in_progress | won | lost | no_response | abandoned
we_quoted_usd:
clearing_price_usd:
loss_reason:
deal_class: []        # e.g. [memory, EOL, automotive-grade]
lessons_tags: []      # e.g. [#market_structure, #spec_discipline]
---

# <Customer> — <QUOTE-NUMBER>

One deal = one SQL row (the numbers) + this file (the story), joined by the quote
number. The agent scaffolds this automatically at close; the human adds the judgment.

## Timeline
- <date>: <event>

## Suppliers approached
| Broker | $/pc | DC | Lead | Status |
|---|---|---|---|---|

## Key insights
-

## Lessons (for cross-deal mining)
- #tag: <one-line learning>

## What we'd do differently
-
