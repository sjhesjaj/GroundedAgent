// @vitest-environment jsdom
//
// The 售后服务中心 in the real CustomerView.vue (jsdom, fetch mocked): the order
// list with every package, the 申请售后 sheet that only drafts, an exchange
// draft without a target that goes through the conversation's clarification,
// a draft prepared during a send that survives its reply, and a refused
// duplicate that leaves the line badge on the request still under review.
import assert from 'node:assert/strict'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, onTestFinished, test, vi } from 'vitest'

import AppTopBar from '../src/components/AppTopBar.vue'
import { TOKEN_STORAGE_KEY } from '../src/auth'
import { useAuthStore } from '../src/stores/auth'
import CustomerView from '../src/views/CustomerView.vue'

async function settle() {
  for (let round = 0; round < 10; round += 1) await flushPromises()
}

const sessionId = 'd'.repeat(32)
const persona = { persona_id: 'demo-a', display_name: '演示顾客甲' }
const BUSINESS_TIME = '2026-11-15T10:00:00+08:00'

const line = (order_item_id: string, product_name: string, category: string, quantity: number, unit_price: string, subtotal: string, sku: string) =>
  ({ order_item_id, sku, product_name, category, quantity, unit_price, subtotal })

// The demo seed of customer A (GET /orders).
const ORDERS = {
  business_time: BUSINESS_TIME,
  orders: [
    { order_id: 'ORD-1004', status: '已发货', paid_at: '2026-11-10T10:00:00+08:00', total_amount: '148.00',
      items: [line('OI-1004-1', '陶瓷马克杯', '家居', 1, '59.00', '59.00', 'SKU-MUG'),
        line('OI-1004-2', '电热水壶', '家电', 1, '89.00', '89.00', 'SKU-KETTLE')],
      packages: [
        { tracking_no: 'SF1004A', carrier: '顺丰速运', status: '已签收', shipped_at: '2026-11-10T18:00:00+08:00',
          delivered_at: '2026-11-12T11:00:00+08:00', last_event_at: '2026-11-12T11:00:00+08:00' },
        { tracking_no: 'YT1004B', carrier: '圆通速递', status: '运输中', shipped_at: '2026-11-12T20:00:00+08:00',
          delivered_at: null, last_event_at: '2026-11-14T08:15:00+08:00' }] },
    { order_id: 'ORD-1003', status: '待付款', paid_at: null, total_amount: '59.00',
      items: [line('OI-1003-1', '陶瓷马克杯', '家居', 1, '59.00', '59.00', 'SKU-MUG')], packages: [] },
    { order_id: 'ORD-1001', status: '已签收', paid_at: '2026-11-01T20:15:00+08:00', total_amount: '298.00',
      items: [line('OI-1001-1', '纯棉T恤 M码', '服装', 2, '99.00', '198.00', 'SKU-TSHIRT-M'),
        line('OI-1001-2', '棉质内衣 L码', '贴身衣物', 1, '100.00', '100.00', 'SKU-UNDERWEAR-L')],
      packages: [{ tracking_no: 'SF1001', carrier: '顺丰速运', status: '已签收', shipped_at: '2026-11-02T16:00:00+08:00',
        delivered_at: '2026-11-05T14:30:00+08:00', last_event_at: '2026-11-05T14:30:00+08:00' }] },
  ],
}

const emptySession = {
  session_id: sessionId, persona, business_time: BUSINESS_TIME, status: 'OPEN', pending_action_id: null,
  messages: [], pending_actions: [], audit: [],
}

function turn(status: string, reply: { kind: string, text: string }, extra: Record<string, unknown> = {}) {
  return { session_id: sessionId, persona, business_time: BUSINESS_TIME, status, pending_action_id: null, reply,
    clarification: null, citations: [], action: null, trace: null, audit: [], ...extra }
}

type Reply = (body: { text: string }) => unknown | Promise<unknown>

/** fetch answering the page's start-up reads; `reply` answers POST …/messages. */
function backend(reply: Reply = () => { throw new Error('nothing should be sent') }) {
  const seen: { call: string, body?: { text: string } }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options = {}) => {
    const call = `${(options.method || 'GET').toUpperCase()} ${url}`
    const body = options.body === undefined ? undefined : JSON.parse(String(options.body))
    seen.push({ call, body })
    if (call === 'GET /api/aftersales/demo') return Response.json({ business_time: BUSINESS_TIME })
    if (call === 'GET /api/aftersales/orders') return Response.json(ORDERS)
    if (call === 'POST /api/aftersales/sessions') return Response.json(emptySession, { status: 201 })
    if (call === `POST /api/aftersales/sessions/${sessionId}/messages`) return Response.json(await reply(body))
    throw new Error(`unexpected request ${call}`)
  })
  return seen
}

async function page() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = { username: 'customer-a', role: 'customer', display_name: '演示顾客甲', persona_id: 'demo-a' }
  const wrapper = mount(CustomerView, { global: { plugins: [pinia] }, attachTo: document.body })
  onTestFinished(() => wrapper.unmount())
  await settle()
  return wrapper
}

const orderCard = (wrapper: VueWrapper, id: string) => wrapper.get(`article[aria-label="订单 ${id}"]`)
const lineOf = (wrapper: VueWrapper, id: string, product: string) =>
  orderCard(wrapper, id).findAll('.line').find((node) => node.text().includes(product))!
const button = (scope: { findAll: VueWrapper['findAll'] }, label: string) =>
  scope.findAll('button').find((node) => node.text().trim() === label)
const inBody = <T extends Element = HTMLElement>(selector: string) => [...document.body.querySelectorAll<T>(selector)]
const composerText = (wrapper: VueWrapper) => (wrapper.get('#message').element as HTMLTextAreaElement).value

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
})

test('an order shows every package it shipped in, the line amounts, and an unpaid order offers only 咨询', async () => {
  backend()
  const wrapper = await page()

  const twoParcels = orderCard(wrapper, 'ORD-1004')
  const packages = twoParcels.findAll('li.package').map((node) => node.text().replace(/\s+/g, ' '))
  assert.equal(packages.length, 2, 'one row per package, not per line')
  assert.match(packages[0], /顺丰速运 SF1004A.*已签收.*11-12 11:00 签收/)
  assert.match(packages[1], /圆通速递 YT1004B.*运输中.*最近更新 11-14 08:15/)
  assert.match(twoParcels.text(), /下单单价 ¥59\.00 × 1/)
  assert.match(twoParcels.text(), /商品小计/)
  assert.match(twoParcels.get('.foot').text(), /订单金额¥148\.00/)

  const unpaid = orderCard(wrapper, 'ORD-1003')
  assert.equal(button(unpaid, '申请售后'), undefined)
  assert.ok(button(unpaid, '咨询'))
  assert.match(unpaid.text(), /订单未付款/)
  assert.equal(unpaid.findAll('li.package').length, 0)
})

test('申请售后 drafts into the composer; nothing is sent until the customer presses 发送', async () => {
  const seen = backend(({ text }) => turn('WAITING_APPROVAL', { kind: 'action', text: '该退货申请需要人工审批，目前正在等待审批；审批通过前不会执行。' }, {
    pending_action_id: 'PA-0123456789ABCDEF',
    action: { action_name: 'create_return', status: 'WAITING_APPROVAL', pending_action_id: 'PA-0123456789ABCDEF',
      guard: { decision: 'REQUIRE_APPROVAL', reason_code: 'risk_policy_requires_approval' },
      arguments: { order_id: 'ORD-1001', order_item_id: 'OI-1001-2', reason_code: 'no_longer_wanted', echoed: text } },
  }))
  const wrapper = await page()

  await button(lineOf(wrapper, 'ORD-1001', '棉质内衣 L码'), '申请售后')!.trigger('click')
  await settle()
  const expected = '我要退货：订单 ORD-1001 的「棉质内衣 L码」，原因：不想要了。'
  assert.equal(inBody('[data-test="draft-preview"]')[0]?.textContent, expected)

  inBody('.use-draft')[0].click()
  await settle()
  assert.equal(composerText(wrapper), expected)
  assert.match(wrapper.get('.attachment').text(), /棉质内衣 L码/)
  assert.ok(!seen.some(({ call }) => call.endsWith('/messages')), 'the sheet never sends')

  await wrapper.get('button.send').trigger('click')
  await settle()
  const sent = seen.filter(({ call }) => call.endsWith('/messages'))
  assert.deepEqual(sent.map(({ body }) => body), [{ text: expected }])
  assert.deepEqual(wrapper.findAll('article[data-status]').map((node) => node.attributes('data-status')), ['WAITING_APPROVAL'])
  assert.match(lineOf(wrapper, 'ORD-1001', '棉质内衣 L码').text(), /售后审核中/, 'the order line shows the request')
  assert.equal(composerText(wrapper), '')
})

test('an exchange draft without a target goes as written, and the clarification keeps the conversation open', async () => {
  const clarification = '为了继续处理，请告诉我：想换成的商品规格。'
  const seen = backend(() => turn('NEEDS_CLARIFICATION', { kind: 'clarification', text: clarification },
    { clarification: { slots: ['target_sku'] } }))
  const wrapper = await page()

  await button(lineOf(wrapper, 'ORD-1001', '纯棉T恤 M码'), '申请售后')!.trigger('click')
  await settle()
  inBody<HTMLButtonElement>('button.kind').find((node) => node.textContent?.includes('换货'))!.click()
  await settle()
  assert.deepEqual(inBody('button.reason').map((node) => node.textContent?.trim()), ['尺码或规格不合适', '质量问题'])
  const expected = '我想换货：订单 ORD-1001 的「纯棉T恤 M码」，原因：尺码或规格不合适。'
  assert.equal(inBody('[data-test="draft-preview"]')[0]?.textContent, expected)

  inBody('.use-draft')[0].click()
  await settle()
  await wrapper.get('button.send').trigger('click')
  await settle()

  assert.deepEqual(seen.filter(({ call }) => call.endsWith('/messages')).map(({ body }) => body), [{ text: expected }],
    'no target is made up')
  assert.match(wrapper.get('.thread').text(), new RegExp(clarification))
  assert.equal(wrapper.findAll('article[data-status]').length, 0, 'nothing was proposed yet')
  assert.match(wrapper.get('.assistant-title').text(), /等待补充信息/)
  assert.equal(wrapper.get('#message').attributes('disabled'), undefined, 'the customer can answer')
})

test('a draft prepared while the previous message is sending stays in the composer', async () => {
  let answer!: (value: unknown) => void
  backend(() => new Promise((resolve) => { answer = resolve }))
  const wrapper = await page()

  const first = '关于订单 ORD-1001 的「棉质内衣 L码」：可以退吗？'
  await button(lineOf(wrapper, 'ORD-1001', '棉质内衣 L码'), '咨询')!.trigger('click')
  await wrapper.get('#message').setValue(first)
  await wrapper.get('button.send').trigger('click')
  await settle()
  assert.match(wrapper.get('.thread').text(), /可以退吗/, 'the message is on its way')

  // While it is in flight, the customer starts on another product.
  await button(lineOf(wrapper, 'ORD-1004', '陶瓷马克杯'), '咨询')!.trigger('click')
  await settle()
  const next = '关于订单 ORD-1004 的「陶瓷马克杯」：'
  assert.equal(composerText(wrapper), next)

  answer(turn('OPEN', { kind: 'answer', text: '贴身衣物拆封后不支持退货。' }))
  await settle()
  assert.match(wrapper.get('.thread').text(), /贴身衣物拆封后不支持退货/)
  assert.equal(composerText(wrapper), next, 'the reply to the first message does not clear the second draft')
  assert.match(wrapper.get('.attachment').text(), /陶瓷马克杯/)
})

test('a duplicate request that is refused leaves the line on the request still under review', async () => {
  const waiting = turn('WAITING_APPROVAL', { kind: 'action', text: '该退货申请需要人工审批，目前正在等待审批；审批通过前不会执行。' }, {
    pending_action_id: 'PA-0123456789ABCDEF',
    action: { action_name: 'create_return', status: 'WAITING_APPROVAL', pending_action_id: 'PA-0123456789ABCDEF',
      guard: { decision: 'REQUIRE_APPROVAL', reason_code: 'risk_policy_requires_approval' },
      arguments: { order_id: 'ORD-1001', order_item_id: 'OI-1001-2', reason_code: 'no_longer_wanted' } },
  })
  const duplicate = turn('WAITING_APPROVAL', { kind: 'action', text: '这件商品已有处理中的售后申请，没有重复提交。' }, {
    pending_action_id: 'PA-0123456789ABCDEF',
    action: { action_name: 'create_return', status: 'DENIED', pending_action_id: null, code: 'pending_request_exists',
      guard: { decision: 'DENY', reason_code: 'pending_request_exists' },
      arguments: { order_id: 'ORD-1001', order_item_id: 'OI-1001-2', reason_code: 'no_longer_wanted' } },
  })
  const replies = [waiting, duplicate]
  backend(() => replies.shift())
  const wrapper = await page()

  for (const text of ['我要退货：订单 ORD-1001 的「棉质内衣 L码」，原因：不想要了。', '再帮我退一次这件棉质内衣。']) {
    await wrapper.get('#message').setValue(text)
    await wrapper.get('button.send').trigger('click')
    await settle()
  }

  const cards = wrapper.findAll('article[data-status]')
  assert.deepEqual(cards.map((node) => node.attributes('data-status')), ['WAITING_APPROVAL', 'DENIED'])
  assert.match(cards[1].text(), /已有处理中的售后申请/, 'the refusal stays in the conversation')
  const badge = lineOf(wrapper, 'ORD-1001', '棉质内衣 L码').get('.badge').text()
  assert.equal(badge, '售后审核中', 'the line keeps the request under review')
})

test('the customer page has no old stylesheet, and 退出登录 in the account menu logs out', async () => {
  backend()
  localStorage.setItem(TOKEN_STORAGE_KEY, 'customer-token')
  const wrapper = await page()
  assert.equal(document.querySelectorAll('style[data-legacy-styles]').length, 0)

  wrapper.getComponent(AppTopBar).vm.$emit('logout')
  await settle()
  assert.equal(useAuthStore().user, null)
  assert.equal(localStorage.getItem(TOKEN_STORAGE_KEY), null)
})
