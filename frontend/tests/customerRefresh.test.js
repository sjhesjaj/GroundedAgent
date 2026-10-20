// The customer page's "刷新状态" on a pending approval, end to end in the real
// App.vue: mounted with Vue's own createRenderer over a tiny in-memory node tree
// (no DOM library), .vue files compiled on import by @vue/compiler-sfc, fetch
// mocked. The click reaches the real ActionCard button, App's refreshSession and
// api.js; the re-render is read back from the tree.
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createRenderer, nextTick } from 'vue'

registerHooks({
  resolve(specifier, context, nextResolve) {
    // Vite resolves extension-less relative imports ('./api'); Node needs '.js'.
    if (/^\.\.?\//.test(specifier) && !/\.[a-z]+$/i.test(specifier)) return nextResolve(`${specifier}.js`, context)
    return nextResolve(specifier, context)
  },
  load(url, context, nextLoad) {
    if (!url.endsWith('.vue')) return nextLoad(url, context)
    const filename = fileURLToPath(url)
    const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
    const id = createHash('sha256').update(filename).digest('hex').slice(0, 8)
    // No static hoisting: stringified static HTML needs a real DOM to insert.
    const script = compileScript(descriptor, { id, inlineTemplate: true, templateOptions: { compilerOptions: { hoistStatic: false } } })
    return { format: 'module', source: script.content, shortCircuit: true }
  },
})

const { default: App } = await import('../src/App.vue')

// -- a minimal host for Vue's renderer ------------------------------------------

function element(tag) {
  return {
    kind: 'element', tag, props: {}, listeners: {}, children: [], parentNode: null, value: '',
    addEventListener() {}, removeEventListener() {}, focus() {}, scrollTo() {}, scrollIntoView() {},
    get lastElementChild() { return [...this.children].reverse().find((child) => child.kind === 'element') ?? null },
  }
}

function detach(node) {
  const parent = node.parentNode
  if (parent) parent.children.splice(parent.children.indexOf(node), 1)
  node.parentNode = null
}

const { createApp } = createRenderer({
  createElement: (tag) => element(tag),
  createText: (text) => ({ kind: 'text', text, parentNode: null }),
  createComment: (text) => ({ kind: 'comment', text, parentNode: null }),
  setText: (node, text) => { node.text = text },
  setElementText: (node, text) => {
    node.children.forEach((child) => { child.parentNode = null })
    node.children = text ? [{ kind: 'text', text, parentNode: node }] : []
  },
  insert: (child, parent, anchor) => {
    detach(child)
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(child)
    else parent.children.splice(index, 0, child)
    child.parentNode = parent
  },
  remove: detach,
  parentNode: (node) => node.parentNode,
  nextSibling: (node) => node.parentNode?.children[node.parentNode.children.indexOf(node) + 1] ?? null,
  patchProp: (node, key, previous, next) => {
    if (/^on[A-Z]/.test(key)) node.listeners[key.slice(2).toLowerCase()] = next
    else if (key === 'value') node.value = next
    else node.props[key] = next
  },
})

const textOf = (node) => (node.kind === 'text' ? node.text : node.kind === 'element' ? node.children.map(textOf).join('') : '')
const findAll = (node, match) => (node.kind !== 'element' ? [] : [
  ...(match(node) ? [node] : []), ...node.children.flatMap((child) => findAll(child, match))])
const cardStatuses = (root) => findAll(root, (node) => node.tag === 'article' && node.props['data-status']).map((node) => node.props['data-status'])
const refreshButtons = (root) => findAll(root, (node) => node.tag === 'button' && textOf(node).trim() === '刷新状态')

async function settle() {
  for (let round = 0; round < 10; round += 1) {
    await new Promise((resolve) => setImmediate(resolve))
    await nextTick()
  }
}

// -- the backend ------------------------------------------------------------------

const sessionId = 'c'.repeat(32)
const pendingId = 'PA-0123456789ABCDEF'
const persona = { persona_id: 'demo-a', display_name: '演示顾客甲' }

function sessionView(actionStatus) {
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

test('a pending approval shows 刷新状态; clicking it re-reads the session and renders the outcome', async (t) => {
  let approved = false
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    requests.push(`${(options.method || 'GET').toUpperCase()} ${url}`)
    if (url === '/api/aftersales/demo') return Response.json({ business_time: '2026-11-15T10:00:00+08:00' })
    if (url === '/api/aftersales/sessions') return Response.json(sessionView('WAITING_APPROVAL'), { status: 201 })
    if (url === `/api/aftersales/sessions/${sessionId}`) return Response.json(sessionView(approved ? 'EXECUTED' : 'WAITING_APPROVAL'))
    throw new Error(`unexpected request ${url}`)
  })

  const root = element('div')
  const app = createApp(App, { user: { username: 'customer-a', role: 'customer', display_name: '演示顾客甲', persona_id: 'demo-a' } })
  app.mount(root)
  t.after(() => app.unmount())
  await settle()

  // Waiting: the hint names the button, and the button is there, enabled.
  assert.deepEqual(requests, ['GET /api/aftersales/demo', 'POST /api/aftersales/sessions'])
  assert.deepEqual(cardStatuses(root), ['WAITING_APPROVAL'])
  assert.match(textOf(root), /待人工审批/)
  assert.match(textOf(root), /点下方「刷新状态」查看结果/)
  const [button] = refreshButtons(root)
  assert.ok(button, 'a pending approval offers 刷新状态')
  assert.equal(refreshButtons(root).length, 1)
  assert.notEqual(button.props.disabled, true)
  assert.equal(findAll(root, (node) => node.tag === 'button' && /批准|拒绝/.test(textOf(node))).length, 0,
    'customers never get approval controls')

  // The operator approves elsewhere; the customer clicks 刷新状态.
  approved = true
  button.listeners.click({ preventDefault() {} })
  await settle()

  assert.deepEqual(requests.slice(2), [`GET /api/aftersales/sessions/${sessionId}`])
  const page = textOf(root)
  assert.deepEqual(cardStatuses(root), ['EXECUTED'])
  assert.match(page, /已执行/)
  assert.match(page, /RC-DEMO-1/)
  assert.match(page, /退货已执行。/)
  assert.doesNotMatch(page, /点下方「刷新状态」/)
  assert.equal(refreshButtons(root).length, 0, 'nothing pending: no refresh hint, no button')
})
