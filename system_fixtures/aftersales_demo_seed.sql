-- After-sales demo seed (rows only; structure is aftersales/schema.sql).
--
-- Entirely fictional data. No real customer, order, or personal information.
-- Loaded into a fresh in-memory database per use; nothing here is persistent.
--
-- Every timestamp is a fixed ISO-8601 literal at +08:00, never generated at
-- load time, and at or before the demo virtual_now (2026-11-15T10:00:00+08:00).
-- customer_id values match the server-side demo personas in aftersales/demo.py.

INSERT INTO orders (order_id, customer_id, status, paid_at, total_amount, updated_at, version) VALUES
    ('ORD-1001', 'CUST-001', '已签收', '2026-11-01T20:15:00+08:00', '298.00', '2026-11-05T14:30:00+08:00', 4),
    ('ORD-1002', 'CUST-001', '已发货', '2026-11-12T09:00:00+08:00', '399.00', '2026-11-13T18:00:00+08:00', 3),
    ('ORD-1003', 'CUST-001', '待付款', NULL, '59.00', '2026-11-14T21:00:00+08:00', 1),
    ('ORD-1004', 'CUST-001', '已发货', '2026-11-10T10:00:00+08:00', '148.00', '2026-11-12T20:00:00+08:00', 3),
    ('ORD-2001', 'CUST-002', '已签收', '2026-11-08T11:20:00+08:00', '128.00', '2026-11-11T16:40:00+08:00', 4),
    ('ORD-2002', 'CUST-002', '已完成', '2026-10-10T10:00:00+08:00', '89.00', '2026-10-25T00:00:00+08:00', 5);

INSERT INTO order_items (order_item_id, order_id, sku, product_name, category, quantity, unit_price, updated_at, version) VALUES
    ('OI-1001-1', 'ORD-1001', 'SKU-TSHIRT-M', '纯棉T恤 M码', '服装', 2, '99.00', '2026-11-01T20:15:00+08:00', 1),
    ('OI-1001-2', 'ORD-1001', 'SKU-UNDERWEAR-L', '棉质内衣 L码', '贴身衣物', 1, '100.00', '2026-11-01T20:15:00+08:00', 1),
    ('OI-1002-1', 'ORD-1002', 'SKU-EARBUDS', '无线蓝牙耳机', '数码', 1, '399.00', '2026-11-12T09:00:00+08:00', 1),
    ('OI-1003-1', 'ORD-1003', 'SKU-MUG', '陶瓷马克杯', '家居', 1, '59.00', '2026-11-14T21:00:00+08:00', 1),
    ('OI-1004-1', 'ORD-1004', 'SKU-MUG', '陶瓷马克杯', '家居', 1, '59.00', '2026-11-10T10:00:00+08:00', 1),
    ('OI-1004-2', 'ORD-1004', 'SKU-KETTLE', '电热水壶', '家电', 1, '89.00', '2026-11-10T10:00:00+08:00', 1),
    ('OI-2001-1', 'ORD-2001', 'SKU-TSHIRT-L', '纯棉T恤 L码', '服装', 1, '99.00', '2026-11-08T11:20:00+08:00', 1),
    ('OI-2001-2', 'ORD-2001', 'SKU-SOCKS', '纯棉袜子', '贴身衣物', 1, '29.00', '2026-11-08T11:20:00+08:00', 1),
    ('OI-2002-1', 'ORD-2002', 'SKU-KETTLE', '电热水壶', '家电', 1, '89.00', '2026-10-10T10:00:00+08:00', 1);

-- ORD-1003 is unpaid and has not shipped, so it has no logistics record.
-- ORD-1004 shipped in two packages: SF1004A is delivered, YT1004B is in transit.
INSERT INTO logistics (tracking_no, order_id, carrier, status, shipped_at, delivered_at, last_event_at, updated_at, version) VALUES
    ('SF1001', 'ORD-1001', '顺丰速运', '已签收', '2026-11-02T16:00:00+08:00', '2026-11-05T14:30:00+08:00', '2026-11-05T14:30:00+08:00', '2026-11-05T14:30:00+08:00', 5),
    ('SF1002', 'ORD-1002', '顺丰速运', '运输中', '2026-11-13T18:00:00+08:00', NULL, '2026-11-14T22:10:00+08:00', '2026-11-14T22:10:00+08:00', 3),
    ('SF1004A', 'ORD-1004', '顺丰速运', '已签收', '2026-11-10T18:00:00+08:00', '2026-11-12T11:00:00+08:00', '2026-11-12T11:00:00+08:00', '2026-11-12T11:00:00+08:00', 4),
    ('YT1004B', 'ORD-1004', '圆通速递', '运输中', '2026-11-12T20:00:00+08:00', NULL, '2026-11-14T08:15:00+08:00', '2026-11-14T08:15:00+08:00', 2),
    ('YT2001', 'ORD-2001', '圆通速递', '已签收', '2026-11-09T15:00:00+08:00', '2026-11-11T16:40:00+08:00', '2026-11-11T16:40:00+08:00', '2026-11-11T16:40:00+08:00', 4),
    ('YT2002', 'ORD-2002', '圆通速递', '已签收', '2026-10-11T09:30:00+08:00', '2026-10-13T12:00:00+08:00', '2026-10-13T12:00:00+08:00', '2026-10-13T12:00:00+08:00', 4);

-- Two SKUs are at zero: a present record with available_qty = 0, not a missing one.
INSERT INTO inventory (sku, available_qty, updated_at, version) VALUES
    ('SKU-TSHIRT-M', 20, '2026-11-13T08:00:00+08:00', 12),
    ('SKU-TSHIRT-L', 0, '2026-11-14T19:30:00+08:00', 9),
    ('SKU-UNDERWEAR-L', 15, '2026-11-10T08:00:00+08:00', 4),
    ('SKU-EARBUDS', 5, '2026-11-12T09:00:00+08:00', 7),
    ('SKU-MUG', 12, '2026-11-14T21:00:00+08:00', 6),
    ('SKU-SOCKS', 0, '2026-11-09T08:00:00+08:00', 3),
    ('SKU-KETTLE', 3, '2026-11-01T08:00:00+08:00', 2);

INSERT INTO after_sales_cases (case_id, order_id, order_item_id, customer_id, type, status, reason, created_at, updated_at, version) VALUES
    ('AS-1001', 'ORD-1001', 'OI-1001-1', 'CUST-001', 'exchange', '已完成', '尺码偏小，换大一码', '2026-11-06T10:00:00+08:00', '2026-11-09T15:00:00+08:00', 3),
    ('AS-2001', 'ORD-2001', 'OI-2001-1', 'CUST-002', 'return', '处理中', '质量问题：衣服开线', '2026-11-12T09:30:00+08:00', '2026-11-13T11:00:00+08:00', 2);
