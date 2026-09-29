---
{
  "policy_id": "apparel-exchange",
  "version": "1",
  "title": "服装换货窗口",
  "rule_type": "exchange_window",
  "scope": [
    "服装"
  ],
  "priority": 20,
  "params": {
    "window_days": 30,
    "start_event": "delivered",
    "counting_rule": "natural_days_from_next_day",
    "utc_offset": "+08:00"
  },
  "effective_from": "2026-01-01T00:00:00+08:00",
  "effective_to": null,
  "source_doc": "apparel-exchange.md",
  "locator": "section:服装换货窗口",
  "provenance": {
    "issuer": "GroundedAgent 演示商城售后服务",
    "revision": "2026-09-policy-1"
  }
}
---

## 服装换货窗口

服装商品吊牌完整且不影响再次销售时，换货咨询窗口为签收次日起算 30 个自然日，按 +08:00 日界线计日。服装规则的业务优先级高于标准换货窗口。
