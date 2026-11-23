// @vitest-environment jsdom
//
// The approval desk in the real OperatorView.vue (jsdom, fetch mocked): the
// session queue, the transcript, 审批详情 with the order line from
// GET …/order-summary, and decisions that are confirmed first and sent only to
// POST …/decision. An order summary that is null or answers 409
// recovery_pending keeps the transcript and the action and offers to read again.
import assert from 'node:assert/strict'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, onTestFinished, test, vi } from 'vitest'

import { useAuthStore } from '../src/stores/auth'
import type { ActionView } from '../src/types'
import OperatorView from '../src/views/OperatorView.vue'

async function settle() {
  for (let round = 0; round < 10; round += 1) await flushPromises()
}

const BUSINESS_TIME = '2026-11-15T10:00:00+08:00'
const WAITING_ID = 'a'.repeat(32)
const OPEN_ID = 'c'.repeat(32)
const BROKEN_ID = 'b'.repeat(32)
const PA = 'PA-4B40C7BBE3CD287F'
const personaA = { persona_id: 'demo-a', display_name: '演示顾客甲' }
const personaB = { persona_id: 'demo-b', display_name: '演示顾客乙' }
const ASK = '我要退货：订单 ORD-1001 的「棉质内衣 L码」，原因：不想要了。'
const PROPOSED = '该退货申请需要人工审批，目前正在等待审批；审批通过前不会执行。'

const waitingAction: ActionView = {
  action_name: 'create_return', status: 'WAITING_APPROVAL', pending_action_id: PA, code: null, receipt: null,
  approval_recorded: false, guard: { decision: 'REQUIRE_APPROVAL', reason_code: 'risk_policy_requires_approval' },
  arguments: { order_id: 'ORD-1001', order_item_id: 'OI-1001-2', reason_code: 'no_longer_wanted' },
}
const executedAction: ActionView = {
  ...waitingAction, status: 'EXECUTED',
  receipt: { receipt_id: 'RC-98395B5B7C97F523', resource_type: 'after_sales_case', resource_id: 'AS6-A1B0243FAE033DBE' },
}
const rejectedAction: ActionView = { ...waitingAction, status: 'REJECTED' }

const event = (event_seq: number, event_name: string, pending: string | null, extra: Record<string, unknown> = {}) => ({
  event_seq, event_name, action_name: 'create_return', pending_action_id: pending, receipt_id: null, phase: 'start',
  decision: null, code: null, approver_ref: null, at: BUSINESS_TIME, ...extra,
})

/** The waiting session as GET /operator/sessions/{id} answers, before and after a decision. */
function waitingSession(action: ActionView = waitingAction) {
  const decided = action.status !== 'WAITING_APPROVAL'
  return {
    session_id: WAITING_ID, persona: personaA, business_time: BUSINESS_TIME,
    status: decided ? 'OPEN' : 'WAITING_APPROVAL', pending_action_id: decided ? null : PA,
    messages: [
      { role: 'customer', text: ASK },
      { role: 'assistant', kind: 'action', text: PROPOSED, action_status: 'WAITING_APPROVAL', pending_action_id: PA },
      ...(decided ? [{ role: 'assistant', kind: 'operator_decision', text: '已提交退货申请（售后单号 AS6-A1B0243FAE033DBE），当前状态：待处理。', action_status: action.status, pending_action_id: PA }] : []),
    ],
    pending_actions: [action],
    audit: [
      event(1, 'guard.evaluated', null, { decision: 'REQUIRE_APPROVAL', code: 'risk_policy_requires_approval' }),
      event(2, 'action.pending_created', PA),
      ...(decided ? [event(3, 'approval.recorded', PA, { phase: 'resume', decision: action.status === 'REJECTED' ? 'REJECT' : 'APPROVE', approver_ref: 'op-demo-1' })] : []),
    ],
  }
}

const SUMMARY = {
  session_id: WAITING_ID, pending_action_id: PA, action_name: 'create_return',
  order: {
    order_id: 'ORD-1001', status: '已签收', paid_at: '2026-11-01T20:15:00+08:00', total_amount: '298.00',
    packages: [{ tracking_no: 'SF1001', carrier: '顺丰速运', status: '已签收', shipped_at: '2026-11-02T16:00:00+08:00',
      delivered_at: '2026-11-05T14:30:00+08:00', last_event_at: '2026-11-05T14:30:00+08:00' }],
  },
  item: { order_item_id: 'OI-1001-2', sku: 'SKU-UNDERWEAR-L', product_name: '棉质内衣 L码', category: '贴身衣物',
    quantity: 1, unit_price: '100.00', subtotal: '100.00' },
}

const list = (waiting: boolean) => ({ items: [
  { session_id: OPEN_ID, persona: personaB, status: 'OPEN', waiting_approval: [], message_count: 3 },
  { session_id: WAITING_ID, persona: personaA, status: waiting ? 'WAITING_APPROVAL' : 'OPEN', waiting_approval: waiting ? [PA] : [], message_count: 2 },
  { session_id: BROKEN_ID, error: 'recovery_pending' },
] })

type Route = (body: unknown) => Response | Promise<Response>
const failure = (status: number, code: string) => Response.json({ detail: { code } }, { status })

/** fetch with the desk's routes; tests replace a route to change what the server says. */
function backend(overrides: Record<string, Route> = {}) {
  const state = { action: waitingAction }
  const seen: { call: string, body: unknown }[] = []
  const routes: Record<string, Route> = {
    'GET /api/aftersales/operator/sessions': () => Response.json(list(state.action.status === 'WAITING_APPROVAL')),
    [`GET /api/aftersales/operator/sessions/${WAITING_ID}`]: () => Response.json(waitingSession(state.action)),
    [`GET /api/aftersales/operator/sessions/${WAITING_ID}/actions/${PA}/order-summary`]: () => Response.json(SUMMARY),
    [`POST /api/aftersales/operator/sessions/${WAITING_ID}/decision`]: (body) => {
      const { decision } = body as { decision: string }
      state.action = decision === 'APPROVE' ? executedAction : rejectedAction
      return Response.json({ ...waitingSession(state.action), reply: { kind: 'operator_decision', text: '…' }, clarification: null,
        citations: [], action: state.action, trace: null, operator_decision: { pending_action_id: PA, decision, approver_ref: 'op-demo-1' } })
    },
    ...overrides,
  }
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options = {}) => {
    const call = `${(options.method || 'GET').toUpperCase()} ${url}`
    const body = options.body === undefined ? undefined : JSON.parse(String(options.body))
    seen.push({ call, body })
    const route = routes[call]
    if (!route) throw new Error(`unexpected request ${call}`)
    return route(body)
  })
  return { seen, state, routes }
}

async function desk() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = { username: 'operator', role: 'operator', display_name: '售后操作员 op-demo-1', persona_id: null }
  const wrapper = mount(OperatorView, { global: { plugins: [pinia] }, attachTo: document.body })
  onTestFinished(() => wrapper.unmount())
  await settle()
  return wrapper
}

const text = (node: { text(): string }) => node.text().replace(/\s+/g, ' ')
/** Text with all whitespace removed: sibling elements are joined without spaces. */
const flat = (value: string | { text(): string }) => (typeof value === 'string' ? value : value.text()).replace(/\s+/g, '')
const tabs = (wrapper: VueWrapper) => wrapper.findAll('.queue [role="tab"]').map(text)
const rows = (wrapper: VueWrapper) => wrapper.findAll('.queue button.row')
const button = (scope: { findAll: VueWrapper['findAll'] }, label: string) =>
  scope.findAll('button').find((node) => node.text().trim() === label)
const inBody = <T extends Element = HTMLElement>(selector: string) => [...document.body.querySelectorAll<T>(selector)]
const dialogButton = (label: string) => inBody<HTMLButtonElement>('.el-dialog button').find((node) => node.textContent?.trim() === label)
const calls = (seen: { call: string, body: unknown }[], suffix: string) => seen.filter(({ call }) => call.endsWith(suffix))

async function open(wrapper: VueWrapper, sessionId = WAITING_ID) {
  await wrapper.get(`.queue button[data-session="${sessionId}"]`).trigger('click')
  await settle()
}

beforeEach(() => {
  document.body.innerHTML = ''
})

test('the queue opens on 待审批; a session shows its transcript and the order line its action names', async () => {
  const { seen } = backend()
  const wrapper = await desk()

  assert.deepEqual(tabs(wrapper), ['待审批1', '进行中1', '全部3'])
  assert.equal(rows(wrapper).length, 1)
  assert.match(text(rows(wrapper)[0]), /演示顾客甲.*1 项待审批/)
  assert.match(text(wrapper.get('.stat.warn')), /待审批 1/)

  await open(wrapper)
  assert.match(wrapper.get('.transcript').text(), new RegExp(ASK))
  assert.match(wrapper.get('.transcript').text(), /智能客服 · 提出退货申请/)
  const review = wrapper.get('.review')
  assert.equal(flat(review.get('.act')), `退货申请待审批${PA}`)
  const goods = flat(review.get('.goods'))
  for (const part of ['棉质内衣 L码', '贴身衣物 · SKU-UNDERWEAR-L', '商品行 OI-1001-2', '下单单价 ¥100.00', '商品小计 ¥100.00',
    '订单 ORD-1001 · 已签收', '订单金额 ¥298.00', '顺丰速运 SF1001 · 已签收 11-05 14:30 签收']) {
    assert.ok(goods.includes(flat(part)), part)
  }
  assert.ok(flat(review).includes(flat('符合条件，需要人工审核按售后规则，这类申请要由售后专员确认后才会执行。REQUIRE_APPROVAL · risk_policy_requires_approval')))
  assert.ok(flat(review.get('details.args')).includes('原因no_longer_wanted·不想要了'))
  assert.ok(button(review, '批准') && button(review, '拒绝'))
  assert.deepEqual(seen.map(({ call }) => call.replace('/api/aftersales', '')), [
    'GET /operator/sessions', `GET /operator/sessions/${WAITING_ID}`, `GET /operator/sessions/${WAITING_ID}/actions/${PA}/order-summary`,
  ], 'reads only; nothing is decided by opening a session')

  await button(review, '处理记录')!.trigger('click')
  assert.deepEqual(wrapper.findAll('.timeline strong').map((node) => node.text()),
    ['规则校验：需人工审核', '已创建待审核申请', '等待操作员审批'])

  await wrapper.findAll('.queue [role="tab"]')[2].trigger('click')
  assert.deepEqual(rows(wrapper).map((node) => node.attributes('data-session')), [WAITING_ID, OPEN_ID, BROKEN_ID], '全部: waiting first')
  assert.match(text(rows(wrapper)[2]), /暂时无法读取/)
})

test('an order summary that is null keeps the transcript and the action, and the decision still goes through', async () => {
  const { seen } = backend({
    [`GET /api/aftersales/operator/sessions/${WAITING_ID}/actions/${PA}/order-summary`]: () =>
      Response.json({ ...SUMMARY, order: null, item: null }),
  })
  const wrapper = await desk()
  await open(wrapper)

  const review = wrapper.get('.review')
  assert.match(review.get('[data-lookup="missing"]').text(), /订单记录里读不到这笔申请的订单/)
  assert.ok(button(review, '重新读取'))
  assert.match(wrapper.get('.transcript').text(), new RegExp(ASK), 'the transcript stays')
  assert.equal(flat(review.get('.act')), `退货申请待审批${PA}`, 'the action stays')
  assert.equal(review.get('details.args').attributes('open'), '', 'the arguments are opened instead')

  await button(review, '批准')!.trigger('click')
  await settle()
  assert.match(inBody('.el-dialog')[0].textContent!, /订单 ORD-1001 · 商品行 OI-1001-2.*订单信息暂时读不到/)
  dialogButton('确认批准')!.click()
  await settle()
  assert.deepEqual(calls(seen, '/decision').map(({ body }) => body), [{ pending_action_id: PA, decision: 'APPROVE' }])
  assert.match(text(wrapper.get('[data-test="result"]')), /已批准 · 申请已提交/)
})

test('409 recovery_pending on the order summary keeps the session on screen and reads again on request', async () => {
  let answers = [failure(409, 'recovery_pending')]
  const { seen } = backend({
    [`GET /api/aftersales/operator/sessions/${WAITING_ID}/actions/${PA}/order-summary`]: () => answers.shift() ?? Response.json(SUMMARY),
  })
  const wrapper = await desk()
  await open(wrapper)

  const review = wrapper.get('.review')
  assert.match(review.get('[data-lookup="recovering"]').text(), /会话正在恢复，订单信息暂时读不到/)
  assert.match(wrapper.get('.transcript').text(), new RegExp(PROPOSED))
  assert.equal(button(review, '批准')!.attributes('disabled'), undefined, 'the decision is not blocked')
  assert.equal(wrapper.find('.notice').exists(), false, 'the page itself has nothing to report')

  answers = []
  await button(review, '重新读取')!.trigger('click')
  await settle()
  assert.equal(wrapper.find('[data-lookup]').exists(), false)
  assert.match(wrapper.get('.goods').text(), /棉质内衣 L码/)
  assert.equal(calls(seen, '/order-summary').length, 2)
  assert.equal(calls(seen, '/decision').length, 0)
})

test('批准 asks first; cancelling sends nothing; confirming sends the decision once and shows what the server kept', async () => {
  const { seen } = backend()
  const wrapper = await desk()
  await open(wrapper)

  await button(wrapper.get('.review'), '批准')!.trigger('click')
  await settle()
  assert.match(inBody('.el-dialog')[0].textContent!, /批准这笔退货申请？/)
  assert.match(inBody('.el-dialog')[0].textContent!, /棉质内衣 L码 × 1.*演示顾客甲 · 订单 ORD-1001 · 商品小计 ¥100\.00/)
  dialogButton('取消')!.click()
  await settle()
  assert.equal(calls(seen, '/decision').length, 0, 'cancel sends nothing')

  await button(wrapper.get('.review'), '批准')!.trigger('click')
  await settle()
  const before = seen.length
  dialogButton('确认批准')!.click()
  await settle()

  assert.deepEqual(seen.slice(before).map(({ call, body }) => [call.replace('/api/aftersales', ''), body]), [
    [`POST /operator/sessions/${WAITING_ID}/decision`, { pending_action_id: PA, decision: 'APPROVE' }],
    ['GET /operator/sessions', undefined],
    [`GET /operator/sessions/${WAITING_ID}`, undefined],
    [`GET /operator/sessions/${WAITING_ID}/actions/${PA}/order-summary`, undefined],
  ])
  const result = text(wrapper.get('[data-test="result"]'))
  assert.match(result, /已批准 · 申请已提交/)
  assert.match(result, /售后单 AS6-A1B0243FAE033DBE · 当前状态：待处理/)
  assert.equal(wrapper.get('[data-test="result"] .receipt').text(), '回执 RC-98395B5B7C97F523')
  assert.match(wrapper.get('.toast').text(), /已批准，售后单 AS6-A1B0243FAE033DBE 已创建（待处理）/)
  assert.equal(button(wrapper.get('.review'), '批准'), undefined, 'nothing left to decide')
  assert.match(wrapper.get('.act').text(), /申请已提交 · 待处理/)
  assert.doesNotMatch(wrapper.text(), /已完成/)
  assert.deepEqual(tabs(wrapper), ['待审批0', '进行中2', '全部3'])
})

test('拒绝 asks first and warns it cannot be undone; the desk then shows the rejection', async () => {
  const { seen } = backend()
  const wrapper = await desk()
  await open(wrapper)

  await button(wrapper.get('.review'), '拒绝')!.trigger('click')
  await settle()
  assert.match(inBody('.el-dialog')[0].textContent!, /拒绝这笔退货申请？.*不能撤回/)
  dialogButton('确认拒绝')!.click()
  await settle()

  assert.deepEqual(calls(seen, '/decision').map(({ body }) => body), [{ pending_action_id: PA, decision: 'REJECT' }])
  assert.match(text(wrapper.get('[data-test="result"]')), /已拒绝 · 未执行.*审核未通过/)
  assert.match(wrapper.get('.toast').text(), /已拒绝这笔申请，未执行/)
})

test('a refused decision is reported and not retried; the desk re-reads what the server kept', async () => {
  const { seen } = backend({
    [`POST /api/aftersales/operator/sessions/${WAITING_ID}/decision`]: () => failure(409, 'decision_refused'),
  })
  const wrapper = await desk()
  await open(wrapper)

  await button(wrapper.get('.review'), '批准')!.trigger('click')
  await settle()
  const before = seen.length
  dialogButton('确认批准')!.click()
  await settle()

  assert.match(wrapper.get('.notice').text(), /本次审批未被接受/)
  assert.deepEqual(seen.slice(before).map(({ call }) => call.replace('/api/aftersales', '')), [
    `POST /operator/sessions/${WAITING_ID}/decision`,
    'GET /operator/sessions',
    `GET /operator/sessions/${WAITING_ID}`,
    `GET /operator/sessions/${WAITING_ID}/actions/${PA}/order-summary`,
  ], 'one attempt, then reads only')
  assert.ok(button(wrapper.get('.review'), '批准'), 'still waiting, as the server says')
})

test('重置演示数据 sits under 更多 and asks before clearing the demo', async () => {
  const { seen, routes } = backend({ 'POST /api/aftersales/demo/reset': () => Response.json({ business_time: BUSINESS_TIME }) })
  const wrapper = await desk()
  await open(wrapper)

  await wrapper.get('button.more').trigger('click')
  await settle()
  inBody('.el-dropdown-menu__item').find((node) => node.textContent?.includes('重置演示数据'))!.click()
  await settle()
  assert.match(inBody('.el-dialog')[0].textContent!, /重置演示数据？.*不能撤回/)
  assert.equal(calls(seen, '/demo/reset').length, 0, 'nothing before the confirmation')

  routes['GET /api/aftersales/operator/sessions'] = () => Response.json({ items: [] })
  dialogButton('确认重置')!.click()
  await settle()
  assert.equal(calls(seen, '/demo/reset').length, 1)
  assert.equal(wrapper.find('.transcript .head').exists(), false, 'the selection is cleared')
  assert.match(wrapper.get('.queue').text(), /暂无会话/)
  assert.match(wrapper.get('.toast').text(), /演示数据已重置/)
})
