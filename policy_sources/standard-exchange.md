---
{
  "policy_id": "standard-exchange",
  "version": "1",
  "title": "标准换货窗口",
  "rule_type": "exchange_window",
  "scope": [],
  "priority": 10,
  "params": {
    "window_days": 15,
    "start_event": "delivered",
    "counting_rule": "natural_days_from_next_day",
    "utc_offset": "+08:00"
  },
  "effective_from": "2026-01-01T00:00:00+08:00",
  "effective_to": null,
  "source_doc": "standard-exchange.md",
  "locator": "section:标准换货窗口",
  "provenance": {
    "issuer": "GroundedAgent 演示商城售后服务",
    "revision": "2026-09-policy-1"
  }
}
---

## 标准换货窗口

符合换货条件的商品，可在签收次日起算 15 个自然日内咨询换货，按 +08:00 日界线计日。换货还需要核对目标规格的当前库存，本规则不代表已经办理换货。
