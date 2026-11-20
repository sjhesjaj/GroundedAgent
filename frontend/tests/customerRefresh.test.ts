// @vitest-environment jsdom
//
// The customer page's "刷新状态" on a pending approval, end to end in the real
// CustomerView.vue: mounted with Vue Test Utils in jsdom, fetch mocked. The
// click reaches the real ActionCard button, the page's refreshSession and
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

test('a pending approval shows 刷新状态; clicking it re-reads the session and renders the outcome', async () => {
  let approved = false
  const requests: string[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options = {}) => {
    requests.push(`${(options.method || 'GET').toUpperCase()} ${url}`)
    if (url === '/api/aftersales/demo') return Response.json({ business_time: '2026-11-15T10:00:00+08:00' })
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
  const refreshButtons = () => wrapper.findAll('button').filter((node) => node.text().trim() === '刷新状态')

  // Waiting: the hint names the button, and the button is there, enabled.
  assert.deepEqual(requests, ['GET /api/aftersales/demo', 'POST /api/aftersales/sessions'])
  assert.deepEqual(cardStatuses(), ['WAITING_APPROVAL'])
  assert.match(wrapper.text(), /待人工审批/)
  assert.match(wrapper.text(), /点下方「刷新状态」查看结果/)
  const [button] = refreshButtons()
  assert.ok(button, 'a pending approval offers 刷新状态')
  assert.equal(refreshButtons().length, 1)
  assert.equal(button.attributes('disabled'), undefined)
  assert.equal(wrapper.findAll('button').filter((node) => /批准|拒绝/.test(node.text())).length, 0,
    'customers never get approval controls')

  // The operator approves elsewhere; the customer clicks 刷新状态.
  approved = true
  await button.trigger('click')
  await settle()

  assert.deepEqual(requests.slice(2), [`GET /api/aftersales/sessions/${sessionId}`])
  const page = wrapper.text()
  assert.deepEqual(cardStatuses(), ['EXECUTED'])
  assert.match(page, /已执行/)
  assert.match(page, /RC-DEMO-1/)
  assert.match(page, /退货已执行。/)
  assert.doesNotMatch(page, /点下方「刷新状态」/)
  assert.equal(refreshButtons().length, 0, 'nothing pending: no refresh hint, no button')
})
