-- PostgreSQL port of aftersales/schema.sql (V2 after-sales business schema).
--
-- Same tables, columns, keys and constraints; only the dialect differs:
--   GLOB '[0-9]*.[0-9][0-9]'   -> ~ '^[0-9].*\.[0-9][0-9]$' (GLOB is anchored; so is this)
--   INTEGER (64-bit in SQLite) -> BIGINT
--   TEXT                       -> TEXT COLLATE "C": byte order, like SQLite's BINARY
--                                 collation, so every ORDER BY sorts as it does there
-- Applied inside a fresh schema that the connection's search_path names.

CREATE TABLE orders (
    order_id     TEXT COLLATE "C" PRIMARY KEY,
    customer_id  TEXT COLLATE "C" NOT NULL,
    status       TEXT COLLATE "C" NOT NULL CHECK (status IN (
                     '待付款', '已付款', '已发货', '已签收', '已完成', '已取消')),
    paid_at      TEXT COLLATE "C",
    total_amount TEXT COLLATE "C" NOT NULL CHECK (total_amount ~ '^[0-9].*\.[0-9][0-9]$'),
    updated_at   TEXT COLLATE "C" NOT NULL,
    version      BIGINT NOT NULL CHECK (version >= 1)
);

CREATE TABLE order_items (
    order_item_id TEXT COLLATE "C" PRIMARY KEY,
    order_id      TEXT COLLATE "C" NOT NULL REFERENCES orders (order_id),
    sku           TEXT COLLATE "C" NOT NULL,
    product_name  TEXT COLLATE "C" NOT NULL,
    category      TEXT COLLATE "C" NOT NULL,
    quantity      BIGINT NOT NULL CHECK (quantity >= 1),
    unit_price    TEXT COLLATE "C" NOT NULL CHECK (unit_price ~ '^[0-9].*\.[0-9][0-9]$'),
    updated_at    TEXT COLLATE "C" NOT NULL,
    version       BIGINT NOT NULL CHECK (version >= 1)
);

-- An order may ship in several packages: order_id -> 0..N tracking numbers.
CREATE TABLE logistics (
    tracking_no   TEXT COLLATE "C" PRIMARY KEY,
    order_id      TEXT COLLATE "C" NOT NULL REFERENCES orders (order_id),
    carrier       TEXT COLLATE "C" NOT NULL,
    status        TEXT COLLATE "C" NOT NULL CHECK (status IN (
                      '运输中', '派送中', '已签收', '异常', '退回')),
    shipped_at    TEXT COLLATE "C" NOT NULL,
    delivered_at  TEXT COLLATE "C",
    last_event_at TEXT COLLATE "C" NOT NULL,
    updated_at    TEXT COLLATE "C" NOT NULL,
    version       BIGINT NOT NULL CHECK (version >= 1)
);

CREATE TABLE inventory (
    sku           TEXT COLLATE "C" PRIMARY KEY,
    available_qty BIGINT NOT NULL CHECK (available_qty >= 0),
    updated_at    TEXT COLLATE "C" NOT NULL,
    version       BIGINT NOT NULL CHECK (version >= 1)
);

CREATE TABLE after_sales_cases (
    case_id       TEXT COLLATE "C" PRIMARY KEY,
    order_id      TEXT COLLATE "C" NOT NULL REFERENCES orders (order_id),
    order_item_id TEXT COLLATE "C" NOT NULL REFERENCES order_items (order_item_id),
    customer_id   TEXT COLLATE "C" NOT NULL,
    type          TEXT COLLATE "C" NOT NULL CHECK (type IN ('return', 'exchange')),
    status        TEXT COLLATE "C" NOT NULL CHECK (status IN (
                      '待处理', '处理中', '已完成', '已拒绝', '已取消')),
    reason        TEXT COLLATE "C" NOT NULL,
    created_at    TEXT COLLATE "C" NOT NULL,
    updated_at    TEXT COLLATE "C" NOT NULL,
    version       BIGINT NOT NULL CHECK (version >= 1)
);
