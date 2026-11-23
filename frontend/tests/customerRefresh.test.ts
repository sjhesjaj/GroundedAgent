// @vitest-environment jsdom
//
// The customer page's "刷新进度" on a pending approval, end to end in the real
// CustomerView.vue: mounted with Vue Test Utils in jsdom, fetch mocked. The
// click reaches the real progress-card button, the page's refreshSession and
// api.ts; the re-render is read back from the DOM.
import assert from 'node:assert/strict'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { onTestFinished, test, vi } from 'vitest'

import { useAuthStore } from '../src/stores/auth'
import CustomerView from '../src/views/CustomerView.vue'

async function settle() {
  for (let round = 0; round < 10; round += 1) await flushPromises()
}

// -- the backend ------------------------------------------------------------------

const sessionId = 'c'.repeat(32)
const pendingId = 'PA-0123456789ABCDEF'
const persona = { persona_id: 'demo-a', display_name: '演示顾客甲' }

const orders = {
  business_time: '2026-11-15T10:00:00+08:00',
  orders: [{
    order_id: 'ORD-1001', status: '已签收', paid_at: '2026-11-01T20:15:00+08:00', total_amount: '298.00',
    items: [{ order_item_id: 'OI-1001-2', sku: 'SKU-UNDERWEAR-L', product_name: '棉质内衣 L码', category: '贴身衣物',
      quantity: 1, unit_price: '100.00', subtotal: '100.00' }],
    packages: [],
  }],
}

function sessionView(actionStatus: string) {
  const waiting = actionStatus === 'WAITING_APPROVAL'
  return {
    session_id: sessionId, persona, business_time: '2026-11-15T10:00:00+08:00',
    status: waiting ? 'WAITING_APPROVAL' : 'OPEN', pending_action_id: waiting ? pendingId : null,
    messages: [
      { role: 'customer', text: '我要退 ORD-1001 里的内衣' },
      { role: 'assistant', text: '退货申请已提交，等待人工审批。', kind: 'action', pending_action_id: pendingId },
      ...(waiting ? [] : [{ role: 'assistant', text: '退货已执行。', kind: 'operator_decision', pending_action_id: pendingId }]),
    ],
    pending_actions: [{
      pending_action_id: pendingId, action_name: 'create_return', status: actionStatus,
      arguments: { order_id: 'ORD-1001', order_item_id: 'OI-1001-2' },
      receipt: waiting ? null : { receipt_id: 'RC-DEMO-1', resource_type: 'after_sales_case', resource_id: 'AS-DEMO-1' },
    }],
    audit: [],
  }
}

test('a pending approval shows 刷新进度; clicking it re-reads the session and renders the outcome', async () => {
  let approved = false
  const requests: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options = {}) => {
    requests.push(`${(options.method || 'GET').toUpperCase()} ${url}`)
    if (url === '/api/aftersales/demo') return Response.json({ business_time: '2026-11-15T10:00:00+08:00' })
    if (url === '/api/aftersales/orders') return Response.json(orders)
    if (url === '/api/aftersales/sessions') return Response.json(sessionView('WAITING_APPROVAL'), { status: 201 })
    if (url === `/api/aftersales/sessions/${sessionId}`) return Response.json(sessionView(approved ? 'EXECUTED' : 'WAITING_APPROVAL'))
    throw new Error(`unexpected request ${url}`)
  })

  const pinia = createPinia()
  setActivePinia(pinia)
  useAuthStore().user = { username: 'customer-a', role: 'customer', display_name: '演示顾客甲', persona_id: 'demo-a' }
  const wrapper = mount(CustomerView, { global: { plugins: [pinia] }, attachTo: document.body })
  onTestFinished(() => wrapper.unmount())
  await settle()

  const cardStatuses = () => wrapper.findAll('article[data-status]').map((node) => node.attributes('data-status'))
  const refreshButtons = () => wrapper.findAll('button').filter((node) => node.text().trim() === '刷新进度')
  const card = () => wrapper.get('article[data-status]')

  // Waiting: the hint names the button, and the button is there, enabled. The
  // order list is read alongside, and only that.
  assert.deepEqual(requests, ['GET /api/aftersales/demo', 'GET /api/aftersales/orders', 'POST /api/aftersales/sessions'])
  assert.deepEqual(cardStatuses(), ['WAITING_APPROVAL'])
  assert.match(card().text(), /待审核/)
  assert.match(card().text(), /棉质内衣 L码/, 'the card names the product from the order list')
  assert.match(wrapper.text(), /审核完成后点「刷新进度」查看结果/)
  const [button] = refreshButtons()
  assert.ok(button, 'a pending approval offers 刷新进度')
  assert.equal(refreshButtons().length, 1)
  assert.equal(button.attributes('disabled'), undefined)
  assert.equal(wrapper.findAll('button').filter((node) => /批准|拒绝/.test(node.text())).length, 0,
    'customers never get approval controls')

  // The operator approves elsewhere; the customer clicks 刷新进度.
  approved = true
  await button.trigger('click')
  await settle()

  assert.deepEqual(requests.slice(3), [`GET /api/aftersales/sessions/${sessionId}`])
  const page = wrapper.text()
  assert.deepEqual(cardStatuses(), ['EXECUTED'])
  // EXECUTED is a created request whose after-sales case is 待处理, not a finished one.
  assert.match(card().text(), /申请已提交 · 待处理/)
  assert.match(card().text(), /售后单号 AS-DEMO-1，当前状态：待处理/)
  assert.doesNotMatch(card().text(), /已完成/)
  assert.match(page, /退货已执行。/)
  assert.doesNotMatch(page, /审核完成后点「刷新进度」/)
  assert.equal(refreshButtons().length, 0, 'nothing pending: no refresh hint, no button')
})
