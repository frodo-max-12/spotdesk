# Market conventions — how the semiconductor spot market actually quotes

Distilled from a read-only study of 574 real threads / 800 messages on the desk's own
mailbox (May–Jul 2026), plus live RFQ flow. Injected into the agent's prompts so it
reasons like this desk, not from generic assumptions. Edit this file to change how the
agent reads the market — it is loaded fresh on every decision.

## What the business is

The desk (a semiconductor components distributor) runs a TWO-SIDED SPOT-TRADING operation:

- **Sourcing side (most of the traffic):** a customer/broker asks for a part → the desk
  fans the SAME RFQ across a panel of HK / China / Singapore / India / EU traders plus
  authorized distributors (Arrow, Avnet, Ingram) → collects COST quotes → marks up →
  quotes back. Fan wide, buy the floor: cost dispersion for the identical part has been
  observed at ~88% ($4.45 → $8.38 across 8 brokers for the same DDR3 lot).
- **Selling side:** the desk also broadcasts its own spot-stock offers (CPUs, DRAM,
  SSD/HDD, GPUs) to prospects and brokers.
- The SAME counterparties often appear on BOTH sides ("we have a customer who needs…").
  Never RFQ the inquiring customer for their own part.

Trade is in USD per piece; the heavy families are memory (DRAM/DDR3/DDR4/DDR5),
Intel/AMD CPUs, SSD/HDD, FPGAs, and broadline ICs.

## How vendors quote (teach the parser these)

- **USD, per piece.** Some EU vendors quote EUR; Indian vendors may quote INR.
- Standard line shape: `Brand | MPN | Qty | D/C | Price | Lead time`
  (e.g. `Broadcom BCM54220SB0IFBG 1,120pcs 23+ USD 32.40 3-4 weeks`).
- **Date code is first-class, written `YY+`** ("that year or newer"); newer = premium.
  Always capture it; always ask for it.
- **Lead time is a stock signal:** "2-3 days" / "ex-stock" = in stock;
  "3-4 weeks" / "allocation" = spot or factory allocation (uncertain).
- **MOQ and SPQ matter** ("SPQ 2K/reel", "MOQ 100 pcs"); higher qty unlocks price tiers,
  so quantity is always stated explicitly.
- Default incoterm **EXW (Ex-Works Hong Kong)**, default payment **T/T**. COO and
  packaging (Tray / Reel / Tube) are commonly stated; non-China COO is preferred.
- Condition phrases: "100% new & original", "factory sealed", "Pb-free / RoHS".
- Vendors **decline bluntly**: "no stock", "no carry", "no bid", "under allocation",
  "not authorised". Record a decline as a No-Bid, never as silence.
- Quotes are perishable ("valid today only", "subject to prior sale"). Capture validity.
- **A suspiciously LOW price is a counterfeit red flag, not a win.** The desk's rule:
  ask for a stock-label photo (date code + packing) and/or manufacturer CoC /
  authorized-distributor packing list before committing. Vendors often refuse until PO —
  that is normal, not evasive.

## How customers inquire

- Lead with an exact **MPN + firm quantity**; ask for stock, unit + tiered price, lead
  time, often date code and "full quantity".
- Urgency is explicit and project-linked ("very urgent, linked to a critical customer
  project"). Mostly single-MPN; multi-line BOMs come from institutional buyers.
- Brokers frame demand as "we have a customer who needs…" and push to lock full qty.
- Customers **negotiate against a benchmark** ("we bought at USD 225, your 260 is high").
- Short, persistent follow-up; they convert to PO fast once price is agreed.
