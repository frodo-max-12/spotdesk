-- SpotDesk — merged schema.
-- SQLite (WAL). Postgres-portable: no SQLite-only syntax beyond AUTOINCREMENT.
--
-- Two lineages merged into one database:
--   * operational tables (emails, deals, vendor_rfqs, vendor_quotes, vendor_pos, ...)
--     — the mechanical RFQ→quote→PO pipeline state.
--   * intelligence tables (counterparties w/ trust, price_history, outcomes on deals,
--     audit_log, system_kpis, outreach ledger) — the compounding layer: every decision
--     leaves a label the next decision can read.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ============================================================
-- emails — every inbound message the agent has processed (idempotency spine).
-- The gmail_id UNIQUE constraint is the dedup marker: it is committed BEFORE any
-- handler sends anything, so a crash mid-handler can never double-send.
-- ============================================================
CREATE TABLE IF NOT EXISTS emails (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_id        TEXT UNIQUE NOT NULL,
    thread_id       TEXT,
    from_email      TEXT,
    from_name       TEXT,
    to_email        TEXT,
    cc              TEXT,                          -- raw Cc header (sender's own team)
    subject         TEXT,
    body_text       TEXT,
    body_html       TEXT,
    received_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    email_type      TEXT DEFAULT 'other',          -- rfq/po/negotiation/vendor_offer/logistics_update/delivery_query/technical/complaint/spam/other
    status          TEXT DEFAULT 'new',            -- new/acknowledged/processing/escalated/archived/done
    urgency         TEXT DEFAULT 'normal',
    has_attachments INTEGER DEFAULT 0,
    attachment_names TEXT,                          -- JSON list
    classification  TEXT,                           -- JSON: raw classifier output
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_emails_thread ON emails(thread_id);
CREATE INDEX IF NOT EXISTS idx_emails_type ON emails(email_type);

-- ============================================================
-- deals — one row per customer inquiry worked (the quotation + its outcome label).
-- line_items is a JSON list; each line carries the full consolidation result
-- (cost, vendor, margin, resale, remark, pricing_status, fulfillment breakdown).
-- The outcome columns are what make a deal TRAINABLE: we_quoted / clearing price /
-- outcome / loss_reason. No-response is stored as 'no_response' (right-censored),
-- never as lost — ~80% of spot RFQs simply ghost.
-- ============================================================
CREATE TABLE IF NOT EXISTS deals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id        INTEGER REFERENCES emails(id),
    quote_number    TEXT UNIQUE,
    customer_name   TEXT,
    customer_email  TEXT,
    customer_cc     TEXT,                          -- their team; CC'd on every reply for this deal
    customer_company TEXT,
    currency        TEXT DEFAULT 'USD',
    currency_assumed INTEGER DEFAULT 0,
    currency_source TEXT,
    margin_percent_override REAL,
    status          TEXT DEFAULT 'awaiting_pricing',
    -- awaiting_pricing / sourcing_vendors / vendor_priced / partially_priced /
    -- quote_drafted / sent / follow_up / negotiation / po_received / po_placed /
    -- in_logistics / delivered / won / lost / no_response / expired
    line_items      TEXT,                          -- JSON list
    draft_email_body TEXT,                         -- last built quote HTML (mirror of the Gmail draft)
    validity_days   INTEGER DEFAULT 30,
    total_amount    REAL,
    negotiation_round INTEGER DEFAULT 0,
    -- Decision-time snapshot (captured when the quote is built — features for later learning)
    decision_snapshot TEXT,                        -- JSON: competing vendor quotes, trust, market check, n_brokers
    -- Outcome labels (the FEEDBACK edge of the compounding loop)
    we_quoted_usd   REAL,                          -- total we quoted
    clearing_price_usd REAL,                       -- what the deal actually closed at (NULL = censored)
    outcome         TEXT,                          -- won / lost / no_response / abandoned (NULL = still open)
    loss_reason     TEXT,
    closed_at       TEXT,
    -- Timing
    sent_at         TEXT,
    followup_1_sent_at TEXT,
    followup_2_sent_at TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status);
CREATE INDEX IF NOT EXISTS idx_deals_customer ON deals(customer_email);
CREATE INDEX IF NOT EXISTS idx_deals_outcome ON deals(outcome);

-- ============================================================
-- counterparties — ONE trust ledger for both sides of the desk.
-- In the spot market the same company is often buyer AND vendor, so buyer trust and
-- vendor routing live on the same row. Trust is the multiplier the router/scorer reads,
-- and outcomes update it in the same transaction (the connected flywheel).
-- ============================================================
CREATE TABLE IF NOT EXISTS counterparties (
    email           TEXT PRIMARY KEY,              -- primary contact (lowercased)
    all_emails      TEXT,                          -- learned contacts, '; ' separated
    name            TEXT,
    company         TEXT,
    domain          TEXT,
    kind            TEXT DEFAULT 'buyer',          -- buyer / vendor / both
    country         TEXT,
    -- vendor routing (used when kind includes vendor)
    brands          TEXT,                          -- JSON list; ["ANY"] = open-market broker
    categories      TEXT,                          -- JSON list
    currency        TEXT DEFAULT 'USD',
    priority        INTEGER,                       -- manual 1..5 (1 best); NULL = use learned
    active          INTEGER DEFAULT 1,
    -- trust + credit (the intelligence layer)
    trust_score     INTEGER DEFAULT 50,            -- 0..100
    credit_limit_usd REAL,
    payment_terms   TEXT,
    blacklisted     INTEGER DEFAULT 0,
    -- learned vendor scorecard
    score           REAL,                          -- 0..100 blended performance
    dynamic_priority INTEGER,                      -- derived 1..5
    rfqs_sent       INTEGER DEFAULT 0,
    rfqs_replied    INTEGER DEFAULT 0,
    quotes_received INTEGER DEFAULT 0,
    quotes_won      INTEGER DEFAULT 0,
    avg_response_hours REAL,
    avg_lead_days   REAL,
    -- buyer stats
    total_quoted_usd REAL DEFAULT 0,
    total_closed_usd REAL DEFAULT 0,
    n_deals_won     INTEGER DEFAULT 0,
    n_deals_lost    INTEGER DEFAULT 0,
    n_inquiries     INTEGER DEFAULT 0,
    first_seen      TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen       TEXT DEFAULT CURRENT_TIMESTAMP,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_cp_kind ON counterparties(kind);
CREATE INDEX IF NOT EXISTS idx_cp_domain ON counterparties(domain);
CREATE INDEX IF NOT EXISTS idx_cp_active ON counterparties(active);

-- ============================================================
-- vendor_rfqs — one row per vendor emailed for a deal (thread tracking)
-- ============================================================
CREATE TABLE IF NOT EXISTS vendor_rfqs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id         INTEGER NOT NULL REFERENCES deals(id),
    -- vendor_email is a soft reference: vendors can be learned mid-flight, and RFQ
    -- history must survive a counterparty row being reshaped.
    vendor_email    TEXT,
    vendor_name     TEXT,
    thread_id       TEXT,
    message_id      TEXT,
    requested_mpns  TEXT,                          -- JSON list
    status          TEXT DEFAULT 'drafted',        -- drafted/sent/partial/replied/no_bid/no_reply/test_sent/suppressed
    sent_at         TEXT,
    replied_at      TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vrfq_deal ON vendor_rfqs(deal_id);
CREATE INDEX IF NOT EXISTS idx_vrfq_thread ON vendor_rfqs(thread_id);

-- ============================================================
-- vendor_quotes — one COST price per part per vendor (parsed from replies)
-- ============================================================
CREATE TABLE IF NOT EXISTS vendor_quotes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id         INTEGER NOT NULL REFERENCES deals(id),
    vendor_rfq_id   INTEGER REFERENCES vendor_rfqs(id),
    vendor_email    TEXT,
    vendor_name     TEXT,
    mpn             TEXT,
    manufacturer    TEXT,
    cost_price      REAL,
    currency        TEXT,
    moq             INTEGER,
    spq             INTEGER,
    offered_qty     INTEGER,                       -- supply cap → drives split-sourcing
    lead_time       TEXT,
    date_code       TEXT,
    packaging       TEXT,                          -- reel/tray/tube/bulk → split homogeneity check
    valid_until     TEXT,                          -- parsed expiry; PO after this → re-source
    validity_raw    TEXT,
    notes           TEXT,
    is_selected     INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vq_deal ON vendor_quotes(deal_id);
CREATE INDEX IF NOT EXISTS idx_vq_mpn ON vendor_quotes(mpn);

-- ============================================================
-- vendor_pos — purchase orders WE place to winning vendors after the customer PO
-- ============================================================
CREATE TABLE IF NOT EXISTS vendor_pos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id         INTEGER NOT NULL REFERENCES deals(id),
    vendor_email    TEXT,
    vendor_name     TEXT,
    po_number       TEXT UNIQUE,
    customer_po_ref TEXT,
    line_items      TEXT,                          -- JSON
    total_cost      REAL,
    currency        TEXT,
    status          TEXT DEFAULT 'drafted',        -- drafted/placed/confirmed/shipped/received/cancelled/test_sent/suppressed
    thread_id       TEXT,
    message_id      TEXT,
    placed_at       TEXT,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_vpo_deal ON vendor_pos(deal_id);
CREATE INDEX IF NOT EXISTS idx_vpo_thread ON vendor_pos(thread_id);

-- ============================================================
-- shipments — logistics state per vendor PO (the coordination leg)
-- ============================================================
CREATE TABLE IF NOT EXISTS shipments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_po_id    INTEGER NOT NULL REFERENCES vendor_pos(id),
    deal_id         INTEGER REFERENCES deals(id),
    status          TEXT DEFAULT 'awaiting_dispatch',  -- awaiting_dispatch/dispatched/in_transit/customs/delivered/exception
    carrier         TEXT,
    awb             TEXT,                          -- tracking / air waybill number
    incoterm        TEXT,
    origin          TEXT,
    destination     TEXT,
    eta             TEXT,
    last_event      TEXT,
    last_event_at   TEXT,
    customer_notified_at TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ship_status ON shipments(status);

-- ============================================================
-- outbound_actions — the drafts-first human-in-the-loop spine.
-- EVERY outbound the agent wants to send becomes a Gmail draft; this table tracks the
-- draft through its life. The agent supersedes stale drafts (delete + recreate) whenever
-- the underlying answer changes, so the Drafts folder always holds the current answer.
-- ============================================================
CREATE TABLE IF NOT EXISTS outbound_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,                 -- ack/vendor_rfq/quote/negotiation_reply/vendor_po/po_ack/followup/logistics_update/cold_email/report
    deal_id         INTEGER REFERENCES deals(id),
    vendor_rfq_id   INTEGER REFERENCES vendor_rfqs(id),
    vendor_po_id    INTEGER REFERENCES vendor_pos(id),
    to_email        TEXT NOT NULL,
    cc              TEXT,
    subject         TEXT,
    body_html       TEXT,
    thread_id       TEXT,                          -- thread the draft is attached to (NULL = new thread)
    gmail_draft_id  TEXT,                          -- Gmail draft id while status='drafted'
    draft_message_id TEXT,                         -- the draft's underlying message id
    status          TEXT DEFAULT 'drafted',        -- drafted/sent/auto_sent/dismissed/superseded/failed/suppressed
    supersedes_id   INTEGER REFERENCES outbound_actions(id),
    sent_gmail_id   TEXT,                          -- message id once sent
    sent_at         TEXT,
    note            TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_oa_status ON outbound_actions(status);
CREATE INDEX IF NOT EXISTS idx_oa_deal ON outbound_actions(deal_id);
CREATE INDEX IF NOT EXISTS idx_oa_kind ON outbound_actions(kind);

-- ============================================================
-- negotiation_rounds — customer target prices per round
-- ============================================================
CREATE TABLE IF NOT EXISTS negotiation_rounds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id         INTEGER NOT NULL REFERENCES deals(id),
    round_number    INTEGER NOT NULL,
    customer_email_id INTEGER REFERENCES emails(id),
    customer_target_prices TEXT,                   -- JSON
    our_response    TEXT,                          -- JSON: per-line {met_by_margin_trim | re_sourced, ask_pct}
    forwarded_at    TEXT,
    revised_quote_at TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- price_history — every price point the desk observes, from ANY source.
-- Vendor quotes, vendor offer blasts, distributor API lookups, closed clearing prices.
-- This is the desk's price memory; the anomaly breaker and quoting read the medians.
-- ============================================================
CREATE TABLE IF NOT EXISTS price_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mpn             TEXT NOT NULL,                 -- stored UPPER
    manufacturer    TEXT,
    price_usd       REAL NOT NULL,
    qty             INTEGER,
    side            TEXT,                          -- vendor_quote/vendor_offer/distributor_list/clearing/our_quote
    source          TEXT,                          -- vendor name / api name / deal quote_number
    date_code       TEXT,
    observed_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ph_mpn ON price_history(mpn);
CREATE INDEX IF NOT EXISTS idx_ph_side ON price_history(side);

-- ============================================================
-- market_offers — standing vendor stock-offer blasts (grounded market intel)
-- ============================================================
CREATE TABLE IF NOT EXISTS market_offers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mpn             TEXT,
    manufacturer    TEXT,
    vendor_email    TEXT,
    vendor_name     TEXT,
    unit_price      REAL,
    currency        TEXT DEFAULT 'USD',
    date_code       TEXT,
    lead_time       TEXT,
    moq             INTEGER,
    offered_qty     INTEGER,
    packaging       TEXT,
    source_gmail_id TEXT,
    source_thread_id TEXT,
    received_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    active          INTEGER DEFAULT 1,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_mo_mpn ON market_offers(mpn);
CREATE INDEX IF NOT EXISTS idx_mo_active ON market_offers(active);

-- ============================================================
-- part_info — MPN → real manufacturer + description cache (grounded identification)
-- ============================================================
CREATE TABLE IF NOT EXISTS part_info (
    mpn             TEXT PRIMARY KEY,              -- stored UPPER
    manufacturer    TEXT,
    description     TEXT,
    category        TEXT,
    datasheet_url   TEXT,
    source          TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- fx_rates — stored FX (used only to normalize a non-USD vendor cost)
-- ============================================================
CREATE TABLE IF NOT EXISTS fx_rates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pair            TEXT DEFAULT 'USDINR',
    base_rate       REAL,
    buffer_percent  REAL DEFAULT 0,
    effective_rate  REAL,
    source          TEXT,
    fetched_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- outreach — cold-email growth engine ledger (per-campaign, resumable, suppression-aware)
-- ============================================================
CREATE TABLE IF NOT EXISTS outreach_campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT UNIQUE NOT NULL,
    subject         TEXT,
    audience        TEXT,
    body_template   TEXT,
    test_sent_to    TEXT,                          -- test-first gate: a proof email must be recorded before --send
    test_sent_at    TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS outreach_sends (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER NOT NULL REFERENCES outreach_campaigns(id),
    to_email        TEXT NOT NULL,
    company         TEXT,
    sku_combo       TEXT,                          -- JSON: which SKUs this recipient saw (anti-forwarding)
    gmail_msg_id    TEXT,
    thread_id       TEXT,
    rfc_message_id  TEXT,                          -- cached for threaded follow-ups
    status          TEXT DEFAULT 'sent',           -- sent/bounced/replied/opted_out
    bounce_class    TEXT,                          -- hard/soft + smtp code
    followup_count  INTEGER DEFAULT 0,
    last_followup_at TEXT,
    sent_at         TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(campaign_id, to_email)
);
CREATE INDEX IF NOT EXISTS idx_os_status ON outreach_sends(status);
CREATE TABLE IF NOT EXISTS suppression_list (
    email           TEXT PRIMARY KEY,
    reason          TEXT,                          -- bounced/do_not_contact/opted_out/domain_blocked
    added_at        TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- classification_feedback — human corrections → few-shot examples (prompt learning)
-- ============================================================
CREATE TABLE IF NOT EXISTS classification_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id        INTEGER REFERENCES emails(id),
    subject         TEXT,
    body_snippet    TEXT,
    from_email      TEXT,
    agent_classification TEXT,
    human_correction TEXT,
    reason          TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- compliance_flags — denied-parties / sanctions hits (audit trail)
-- ============================================================
CREATE TABLE IF NOT EXISTS compliance_flags (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    counterparty_email TEXT,
    list_source     TEXT,
    matched_name    TEXT,
    severity        TEXT,
    action_required TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TEXT
);

-- ============================================================
-- audit_log — every decision the system takes, human or agent
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT DEFAULT CURRENT_TIMESTAMP,
    actor           TEXT NOT NULL,                 -- agent/human/cron/model
    action          TEXT NOT NULL,
    details_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- ============================================================
-- ingest_state — incremental watermarks (mailbox poll, backfill floor)
-- ============================================================
CREATE TABLE IF NOT EXISTS ingest_state (
    source          TEXT PRIMARY KEY,
    last_msg_id     TEXT,
    last_email_date TEXT,
    last_run_at     TEXT,
    n_processed     INTEGER DEFAULT 0
);

-- ============================================================
-- system_kpis — daily snapshots the distill job writes (dashboard charts)
-- ============================================================
CREATE TABLE IF NOT EXISTS system_kpis (
    date            TEXT PRIMARY KEY,
    open_deals      INTEGER DEFAULT 0,
    drafts_pending  INTEGER DEFAULT 0,
    quotes_sent     INTEGER DEFAULT 0,
    rfqs_sent       INTEGER DEFAULT 0,
    pos_placed      INTEGER DEFAULT 0,
    deals_won       INTEGER DEFAULT 0,
    deals_lost      INTEGER DEFAULT 0,
    revenue_won_usd REAL DEFAULT 0,
    margin_won_usd  REAL DEFAULT 0,
    cold_sends      INTEGER DEFAULT 0,
    bounce_rate_pct REAL
);
