-- V2 Stage 6 action schema (docs/v2/stage6-design.md §7.2, frozen at tag v2-stage6-design).
--
-- Applied ONLY by Stage 6 databases, after aftersales/schema.sql. The Stage 4/5
-- runtimes never load this file, so their five-table read-only database and its
-- content hash are unchanged.
--
-- Every timestamp is written by the ActionGateway from the transaction's single
-- business instant (txn_now); nothing here computes a time at load or write time.

-- Stage 6 business extension: SKU variant groups, the only source of exchange
-- compatibility. A SKU without a row is compatible with nothing.
CREATE TABLE sku_variants (
    sku           TEXT PRIMARY KEY,
    variant_group TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    version       INTEGER NOT NULL CHECK (version >= 1)
);

-- Human handoff tickets: a real persisted record, not a message.
CREATE TABLE human_handoff_tickets (
    ticket_id       TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL REFERENCES orders (order_id),
    order_item_id   TEXT NOT NULL REFERENCES order_items (order_item_id),
    handoff_trigger TEXT NOT NULL CHECK (handoff_trigger IN ('quality_dispute')),
    status          TEXT NOT NULL CHECK (status IN ('待处理', '处理中', '已关闭')),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    version         INTEGER NOT NULL CHECK (version >= 1)
);

-- Pending actions: everything needed to resume after a restart. Stage 6.1
-- declares the table; only Stage 6.2 writes it.
CREATE TABLE pending_actions (
    pending_action_id    TEXT PRIMARY KEY,
    idempotency_key      TEXT NOT NULL UNIQUE,
    request_id           TEXT NOT NULL,
    persona_id           TEXT NOT NULL,
    action_name          TEXT NOT NULL CHECK (action_name IN
                             ('create_return', 'create_exchange', 'escalate_to_human')),
    args_json            TEXT NOT NULL,
    args_sha256          TEXT NOT NULL,
    target_order_id      TEXT NOT NULL,
    target_order_item_id TEXT NOT NULL,
    status               TEXT NOT NULL CHECK (status IN ('PENDING_APPROVAL', 'APPROVED',
                             'REJECTED', 'EXECUTED', 'STALE', 'DENIED', 'FAILED')),
    guard_decision       TEXT NOT NULL CHECK (guard_decision = 'REQUIRE_APPROVAL'),
    guard_reason_code    TEXT NOT NULL,
    snapshot_json        TEXT NOT NULL,
    snapshot_sha256      TEXT NOT NULL,
    action_spec_version  TEXT NOT NULL,
    risk_policy_version  TEXT NOT NULL,
    policy_build_id      TEXT NOT NULL,
    approval_decision    TEXT CHECK (approval_decision IN ('APPROVE', 'REJECT')),
    approver_ref         TEXT,
    decided_at           TEXT,
    outcome_code         TEXT,
    receipt_id           TEXT UNIQUE,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    version              INTEGER NOT NULL CHECK (version >= 1),
    CHECK ((approval_decision IS NULL) = (approver_ref IS NULL)),
    CHECK ((approval_decision IS NULL) = (decided_at IS NULL)),
    CHECK (status <> 'PENDING_APPROVAL' OR approval_decision IS NULL),
    CHECK (status NOT IN ('APPROVED', 'EXECUTED', 'STALE', 'DENIED', 'FAILED')
           OR approval_decision = 'APPROVE'),
    CHECK (status <> 'REJECTED' OR approval_decision = 'REJECT'),
    CHECK ((status = 'EXECUTED') = (receipt_id IS NOT NULL)),
    CHECK ((status IN ('PENDING_APPROVAL', 'APPROVED', 'EXECUTED')) = (outcome_code IS NULL))
);

-- Execution receipts: exactly one per EXECUTED action, immutable, an idempotency anchor.
CREATE TABLE action_receipts (
    receipt_id          TEXT PRIMARY KEY,
    idempotency_key     TEXT NOT NULL UNIQUE,
    request_id          TEXT NOT NULL,
    persona_id          TEXT NOT NULL,
    action_name         TEXT NOT NULL CHECK (action_name IN
                            ('create_return', 'create_exchange', 'escalate_to_human')),
    args_json           TEXT NOT NULL,
    args_sha256         TEXT NOT NULL,
    result_status       TEXT NOT NULL CHECK (result_status = 'EXECUTED'),
    resource_type       TEXT NOT NULL CHECK (resource_type IN
                            ('after_sales_case', 'human_handoff_ticket')),
    resource_id         TEXT NOT NULL,
    pending_action_id   TEXT UNIQUE REFERENCES pending_actions (pending_action_id),
    guard_decision      TEXT NOT NULL CHECK (guard_decision IN ('ALLOW', 'REQUIRE_APPROVAL')),
    guard_reason_code   TEXT NOT NULL,
    snapshot_json       TEXT NOT NULL,
    snapshot_sha256     TEXT NOT NULL,
    action_spec_version TEXT NOT NULL,
    risk_policy_version TEXT NOT NULL,
    policy_build_id     TEXT NOT NULL,
    executed_at         TEXT NOT NULL,
    UNIQUE (resource_type, resource_id),
    CHECK ((action_name = 'escalate_to_human') = (resource_type = 'human_handoff_ticket')),
    CHECK ((guard_decision = 'REQUIRE_APPROVAL') = (pending_action_id IS NOT NULL))
);

-- Append-only audit events (§17); business time; not part of final-state comparison.
CREATE TABLE action_audit_events (
    event_seq         INTEGER PRIMARY KEY,
    event_name        TEXT NOT NULL CHECK (event_name IN (
                          'action.replay_hit', 'guard.evaluated', 'guard.failed',
                          'action.pending_created', 'approval.recorded', 'approval.conflict',
                          'resume.started', 'resume.version_check', 'action.executed',
                          'action.not_executed', 'transaction.rolled_back')),
    request_id        TEXT NOT NULL,
    persona_id        TEXT NOT NULL,
    action_name       TEXT NOT NULL,
    idempotency_key   TEXT,
    pending_action_id TEXT,
    receipt_id        TEXT,
    phase             TEXT CHECK (phase IN ('start', 'resume')),
    decision          TEXT,
    code              TEXT,
    approver_ref      TEXT,
    at                TEXT NOT NULL
);

-- Database-level business invariants: the second line of defence behind the Guard.
CREATE UNIQUE INDEX s6_one_active_case_per_item
    ON after_sales_cases (order_item_id) WHERE status IN ('待处理', '处理中');
CREATE UNIQUE INDEX s6_one_open_pending_per_item
    ON pending_actions (target_order_item_id) WHERE status IN ('PENDING_APPROVAL', 'APPROVED');
CREATE UNIQUE INDEX s6_one_open_ticket_per_item_trigger
    ON human_handoff_tickets (order_item_id, handoff_trigger) WHERE status IN ('待处理', '处理中');
