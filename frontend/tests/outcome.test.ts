// What the progress card says for each action status, and the drafts the
// 申请售后 sheet writes. Pure functions: utils/outcome.ts and utils/draft.ts.
import assert from 'node:assert/strict'
import { test } from 'vitest'

import type { ActionStatus, ActionView } from '../src/types'
import { composeDraft, REASONS_FOR } from '../src/utils/draft'
import { DENY_COPY, describeAction, guardVerdict, lineBadges } from '../src/utils/outcome'

function action(status: ActionStatus, extra: Partial<ActionView> = {}): ActionView {
  return {
    action_name: 'create_return', status, pending_action_id: 'PA-0123456789ABCDEF',
    arguments: { order_id: 'ORD-1001', order_item_id: 'OI-1001-2' }, ...extra,
  }
}

const receipt = (resource_type: string, resource_id: string) => ({ receipt_id: 'RC-1', resource_type, resource_id })

test('EXECUTED means the request was created and the case is 待处理, never 已完成', () => {
  const returned = describeAction(action('EXECUTED', { receipt: receipt('after_sales_case', 'AS6-A1'), approval_recorded: true }))
  assert.equal(returned.tag, '申请已提交 · 待处理')
  assert.equal(returned.summary, '售后单号 AS6-A1，当前状态：待处理。')
  assert.deepEqual(returned.steps.map((step) => step.title), ['提出申请', '规则校验', '人工审核', '申请已提交 · 待处理'])
  assert.deepEqual(returned.next, [])

  const exchanged = describeAction(action('EXECUTED', { action_name: 'create_exchange', receipt: receipt('after_sales_case', 'AS6-B2') }))
  assert.equal(exchanged.tag, '申请已提交 · 待处理')
  assert.match(exchanged.summary, /换货申请提交后不会立即发货/)
  assert.deepEqual(exchanged.steps.map((step) => step.title), ['提出申请', '规则校验', '申请已提交 · 待处理'],
    'no approval step when the Guard allowed it directly')

  const handoff = describeAction(action('EXECUTED', { action_name: 'escalate_to_human', receipt: receipt('handoff_ticket', 'HT-9') }))
  assert.equal(handoff.tag, '工单已创建 · 待跟进')
  assert.match(handoff.summary, /HT-9/)

  for (const outcome of [returned, exchanged, handoff]) {
    assert.doesNotMatch(`${outcome.tag}${outcome.summary}`, /已完成/)
  }
})

test('every other status says what happened and what the customer can do next', () => {
  const waiting = describeAction(action('WAITING_APPROVAL'))
  assert.equal(waiting.tag, '待审核')
  assert.ok(waiting.pulse)
  assert.deepEqual(waiting.next.map((step) => step.kind), ['refresh'])
  assert.match(waiting.hint, /刷新进度/)

  const rejected = describeAction(action('REJECTED'))
  assert.equal(rejected.tag, '审核未通过')
  assert.match(rejected.summary, /没有执行/)
  assert.deepEqual(rejected.next.map((step) => step.kind), ['human'])

  const stale = describeAction(action('STALE'))
  assert.equal(stale.tag, '已失效')
  assert.match(stale.summary, /状态发生了变化，原申请没有执行/)
  assert.deepEqual(stale.next.map((step) => step.kind), ['reapply'])

  const failed = describeAction(action('FAILED'))
  assert.equal(failed.tag, '提交失败')
  assert.match(failed.summary, /申请没有提交/)
  assert.deepEqual(failed.next.map((step) => step.kind), ['reapply', 'human'])

  const denied = describeAction(action('DENIED', { guard: { decision: 'DENY', reason_code: 'not_delivered' } }))
  assert.equal(denied.tag, '不符合办理条件')
  assert.equal(denied.summary, '订单尚未签收，暂时无法提交该申请。申请没有提交。')
  assert.equal(denied.hint, '包裹签收后可以再申请。')

  for (const outcome of [waiting, rejected, stale, failed, denied]) assert.ok(outcome.badge, 'each status has an order-list badge')
})

test('every Guard DENY reason has its own wording and next step', () => {
  // aftersales/guard.py DENY_REASON_CODES
  const codes = [
    'order_not_accessible', 'order_item_not_in_order', 'business_state_conflict', 'order_status_ineligible',
    'not_delivered', 'delivery_not_established', 'active_after_sales_case_exists', 'item_already_returned',
    'pending_request_exists', 'handoff_required', 'non_returnable', 'no_applicable_policy', 'policy_conflict',
    'return_window_closed', 'exchange_window_closed', 'exchange_target_invalid', 'exchange_target_incompatible',
    'inventory_unavailable', 'handoff_not_required', 'handoff_ticket_exists',
  ]
  assert.deepEqual(Object.keys(DENY_COPY).sort(), [...codes].sort())
  for (const code of codes) {
    const outcome = describeAction(action('DENIED', { guard: { decision: 'DENY', reason_code: code } }))
    assert.ok(outcome.summary && outcome.hint, code)
  }
  // A duplicate already says nothing new was submitted; it is not said twice.
  const duplicate = describeAction(action('DENIED', { code: 'pending_request_exists' }))
  assert.equal(duplicate.summary, '这件商品已有处理中的售后申请，没有重复提交。')
  assert.deepEqual(duplicate.next, [])
  // A handoff that was refused does not offer another handoff, and says the ticket was not created.
  const handoff = describeAction(action('DENIED', { action_name: 'escalate_to_human', code: 'handoff_not_required' }))
  assert.match(handoff.summary, /工单没有创建/)
  assert.ok(!handoff.next.some((step) => step.kind === 'human'))
})

test('a line badge follows the request that is still open, not the latest refusal', () => {
  const line = { order_id: 'ORD-1001', order_item_id: 'OI-1001-2' }
  const duplicate = action('DENIED', { pending_action_id: null, arguments: line, guard: { decision: 'DENY', reason_code: 'pending_request_exists' } })

  assert.deepEqual(lineBadges([action('WAITING_APPROVAL', { arguments: line }), duplicate]),
    { 'ORD-1001': { 'OI-1001-2': { text: '售后审核中', tone: 'amber' } } })
  assert.deepEqual(lineBadges([action('EXECUTED', { arguments: line }), duplicate])['ORD-1001']['OI-1001-2'],
    { text: '售后单待处理', tone: 'green' })
  // Only the refusal in this session: the line says a request is already open elsewhere.
  assert.deepEqual(lineBadges([duplicate])['ORD-1001']['OI-1001-2'], { text: '已有申请处理中', tone: 'amber' })
  // Among closed outcomes the later one wins: a new try after a rejection.
  assert.equal(lineBadges([action('REJECTED', { arguments: line }), action('DENIED', { arguments: line, code: 'return_window_closed' })])
    ['ORD-1001']['OI-1001-2'].text, '不符合条件')
  // Other lines keep their own badges.
  const other = lineBadges([action('WAITING_APPROVAL', { arguments: line }), action('FAILED', { arguments: { order_id: 'ORD-1004', order_item_id: 'OI-1004-1' } })])
  assert.equal(other['ORD-1004']['OI-1004-1'].text, '提交失败')
})

test('the Guard verdict reads as words in the evidence drawer', () => {
  assert.equal(guardVerdict(action('WAITING_APPROVAL', { guard: { decision: 'REQUIRE_APPROVAL', reason_code: 'risk_policy_requires_approval' } }))?.title,
    '符合条件，需要人工审核')
  assert.equal(guardVerdict(action('EXECUTED', { guard: { decision: 'ALLOW', reason_code: 'risk_policy_allows' } }))?.title, '符合条件')
  assert.equal(guardVerdict(action('DENIED', { guard: { decision: 'DENY', reason_code: 'non_returnable' } }))?.text,
    '该商品属于不支持退货的品类。')
  assert.equal(guardVerdict(null), null)
})

test('drafts use the backend reason codes and never invent an exchange target', () => {
  const target = { order_id: 'ORD-1001', product_name: '棉质内衣 L码' }
  assert.equal(composeDraft('return', target, 'no_longer_wanted'), '我要退货：订单 ORD-1001 的「棉质内衣 L码」，原因：不想要了。')
  assert.equal(composeDraft('return', target, 'quality_issue', '线头开了'), '我要退货：订单 ORD-1001 的「棉质内衣 L码」，原因：质量问题。线头开了。')

  // No target given: the sentence stops at the reason, and the agent asks (a clarification turn).
  const bare = composeDraft('exchange', { order_id: 'ORD-1001', product_name: '纯棉T恤 M码' }, 'size_or_spec_mismatch')
  assert.equal(bare, '我想换货：订单 ORD-1001 的「纯棉T恤 M码」，原因：尺码或规格不合适。')
  assert.doesNotMatch(bare, /SKU|想换成/)
  assert.equal(composeDraft('exchange', { order_id: 'ORD-1001', product_name: '纯棉T恤 M码' }, 'size_or_spec_mismatch', 'L 码'),
    '我想换货：订单 ORD-1001 的「纯棉T恤 M码」，原因：尺码或规格不合适。想换成：L 码。')

  assert.equal(composeDraft('human', target, null), '我需要人工协助：订单 ORD-1001 的「棉质内衣 L码」。')
  assert.deepEqual(REASONS_FOR.exchange, ['size_or_spec_mismatch', 'quality_issue'], 'an exchange has no 不想要了')
  assert.deepEqual(REASONS_FOR.human, [])
})
