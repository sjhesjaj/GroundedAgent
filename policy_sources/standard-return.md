---
{
  "policy_id": "standard-return",
  "version": "1",
  "title": "标准退货窗口",
  "rule_type": "return_window",
  "scope": [],
  "priority": 10,
  "params": {
    "window_days": 7,
    "start_event": "delivered",
    "counting_rule": "natural_days_from_next_day",
    "utc_offset": "+08:00"
  },
  "effective_from": "2026-01-01T00:00:00+08:00",
  "effective_to": null,
  "source_doc": "standard-return.md",
  "locator": "section:标准退货窗口",
  "provenance": {
    "issuer": "GroundedAgent 演示商城售后服务",
    "revision": "2026-09-policy-1"
  }
}
---

## 标准退货窗口

商品保持完好且符合退货条件时，标准退货申请窗口为签收次日起算 7 个自然日，按 +08:00 日界线计日。签收当日属于窗口内；最后一日结束后窗口关闭。特殊品类限制及人工处理条件需同时核对。
