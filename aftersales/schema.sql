-- V2 after-sales business schema (docs/v2/stage4-design.md §3.1).
--
-- Structure only: no rows, no defaults computed at load time. Every timestamp
-- is an ISO-8601 string with an explicit offset (+08:00 in the demo seed), and
-- every record carries an integer state `version` (>= 1, +1 per write).
--
-- customer_id is a lookup predicate, never a queryable entity: there is no
-- customer table. It comes only from the trusted server-side context.

CREATE TABLE orders (
    order_id     TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN (
                     '待付款', '已付款', '已发货', '已签收', '已完成', '已取消')),
    paid_at      TEXT,
    total_amount TEXT NOT NULL CHECK (total_amount GLOB '[0-9]*.[0-9][0-9]'),
    updated_at   TEXT NOT NULL,
    version      INTEGER NOT NULL CHECK (version >= 1)
);

CREATE TABLE order_items (
    order_item_id TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL REFERENCES orders (order_id),
    sku           TEXT NOT NULL,
    product_name  TEXT NOT NULL,
    category      TEXT NOT NULL,
    quantity      INTEGER NOT NULL CHECK (quantity >= 1),
    unit_price    TEXT NOT NULL CHECK (unit_price GLOB '[0-9]*.[0-9][0-9]'),
    updated_at    TEXT NOT NULL,
    version       INTEGER NOT NULL CHECK (version >= 1)
);

-- An order may ship in several packages: order_id -> 0..N tracking numbers.
CREATE TABLE logistics (
    tracking_no   TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL REFERENCES orders (order_id),
    carrier       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN (
                      '运输中', '派送中', '已签收', '异常', '退回')),
    shipped_at    TEXT NOT NULL,
    delivered_at  TEXT,
    last_event_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    version       INTEGER NOT NULL CHECK (version >= 1)
);

CREATE TABLE inventory (
    sku           TEXT PRIMARY KEY,
    available_qty INTEGER NOT NULL CHECK (available_qty >= 0),
    updated_at    TEXT NOT NULL,
    version       INTEGER NOT NULL CHECK (version >= 1)
);

CREATE TABLE after_sales_cases (
    case_id       TEXT PRIMARY KEY,
    order_id      TEXT NOT NULL REFERENCES orders (order_id),
    order_item_id TEXT NOT NULL REFERENCES order_items (order_item_id),
    customer_id   TEXT NOT NULL,
    type          TEXT NOT NULL CHECK (type IN ('return', 'exchange')),
    status        TEXT NOT NULL CHECK (status IN (
                      '待处理', '处理中', '已完成', '已拒绝', '已取消')),
    reason        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    version       INTEGER NOT NULL CHECK (version >= 1)
);
