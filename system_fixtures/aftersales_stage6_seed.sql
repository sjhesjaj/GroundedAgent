-- Stage 6 seed rows (structure: aftersales/action_schema.sql; docs/v2/stage6-design.md §7.1).
--
-- Applied after aftersales/schema.sql, the base demo seed and the action schema.
-- Only sku_variants rows. The two T-shirt sizes form the group TSHIRT; every
-- other seed SKU is its own single-member group, named after the SKU itself.
-- Compatibility is read from these rows only, never parsed from SKU strings.
-- Timestamps are fixed literals at +08:00, at or before the demo virtual_now.

INSERT INTO sku_variants (sku, variant_group, updated_at, version) VALUES
    ('SKU-TSHIRT-M', 'TSHIRT', '2026-11-01T00:00:00+08:00', 1),
    ('SKU-TSHIRT-L', 'TSHIRT', '2026-11-01T00:00:00+08:00', 1),
    ('SKU-UNDERWEAR-L', 'SKU-UNDERWEAR-L', '2026-11-01T00:00:00+08:00', 1),
    ('SKU-EARBUDS', 'SKU-EARBUDS', '2026-11-01T00:00:00+08:00', 1),
    ('SKU-MUG', 'SKU-MUG', '2026-11-01T00:00:00+08:00', 1),
    ('SKU-SOCKS', 'SKU-SOCKS', '2026-11-01T00:00:00+08:00', 1),
    ('SKU-KETTLE', 'SKU-KETTLE', '2026-11-01T00:00:00+08:00', 1);
