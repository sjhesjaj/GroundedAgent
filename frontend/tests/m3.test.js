import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import * as Vue from 'vue'
import { parse, compileScript } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import { aftersalesApi, AftersalesApiError } from '../src/api.js'
import { openSession, rememberedSessions, SESSION_STORAGE_KEY } from '../src/sessionMemory.js'

// Render the real component with Vue's compiler and server renderer, so these
// assertions cover escaped customer-facing output, not a second formatter.
const source = readFileSync(new URL('../src/components/AgentDetails.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const compiled = compileScript(descriptor, { id: 'm3-agent-details', inlineTemplate: true }).content
const executable = compiled.replace(/import\s*\{([^}]+)\}\s*from\s*['"]vue['"];?/g,
  (_, names) => `const {${names.replace(/\s+as\s+/g, ':')}} = Vue;`).replace('export default', 'return')
const AgentDetails = new Function('Vue', executable)(Vue)
const render = (props) => renderToString(Vue.createSSRApp(AgentDetails, props))

test('KB citations show the document title, version and section', async () => {
  const html = await render({ citations: [{ ref: 'evidence:internal', producer: 'search_knowledge_base',
    source_type: 'document', doc_id: 'kb-return-shipping', title: '退货运费承担说明',
    version: '1', section: '非质量原因退货', locator: 'kb:kb-return-shipping#非质量原因退货' }] })
  for (const text of ['退货运费承担说明', '知识库', '版本 1', '非质量原因退货', 'kb-return-shipping']) assert.ok(html.includes(text))
  assert.ok(!html.includes('evidence:internal'))
})

test('structured and business citations retain their producer and locator', async () => {
  const html = await render({ citations: [{ ref: 'business:order-status', producer: 'get_order',
    source_type: 'business', locator: 'order:ORD-1001/status' }] })
  for (const text of ['business:order-status', 'get_order', 'business', 'order:ORD-1001/status']) assert.ok(html.includes(text))
})

test('citation labels are rendered as text and cannot inject markup', async () => {
  const html = await render({ citations: [{ ref: 'safe', producer: 'search_knowledge_base',
    title: '<script>alert("unsafe")</script>', section: '<img src=x onerror=alert(1)>', doc_id: 'kb-safe' }] })
  assert.ok(!html.includes('<script>'))
  assert.ok(!html.includes('<img'))
  assert.ok(html.includes('&lt;script&gt;'))
  assert.ok(html.includes('&lt;img'))
})

test('generation and decision calls display real usage and unknown usage separately', async () => {
  const html = await render({ trace: { steps: [], model_calls: [
    { kind: 'decision', prompt_tokens: 200, completion_tokens: 12, latency_seconds: 0.5, status: 'success' },
    { kind: 'generation', prompt_tokens: 321, completion_tokens: 45, latency_seconds: 1.25, status: 'protocol_error' },
    { kind: 'generation', prompt_tokens: null, completion_tokens: null, latency_seconds: null, status: 'provider_error' },
  ] } })
  for (const text of ['处理决策', '回复生成', '输入 321', '输出 45', '1.25 秒', '回复校验失败', '调用失败', '输入 —']) assert.ok(html.includes(text))
})

test('409 policy mismatch uses a new-session notice and makes only one request', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(
    { detail: { code: 'policy_version_mismatch' } }, { status: 409 }))
  await assert.rejects(aftersalesApi.sendMessage('a'.repeat(32), '进度怎么样'), (error) => {
    assert.equal(error.status, 409)
    assert.equal(error.code, 'policy_version_mismatch')
    assert.match(error.message, /新建会话/)
    return true
  })
  assert.equal(fetch.mock.callCount(), 1)
})

test('a remembered policy mismatch does not create or migrate a session automatically', async () => {
  const sessionId = 'a'.repeat(32)
  const values = new Map([[SESSION_STORAGE_KEY, sessionId]])
  const target = { sessionStorage: { getItem: (key) => values.get(key), removeItem: (key) => values.delete(key) } }
  let creates = 0
  const api = { session: async () => { throw new AftersalesApiError('请新建会话', 409, 'policy_version_mismatch') },
    createSession: async () => { creates += 1 } }
  await assert.rejects(openSession(api, 'demo-a', target), (error) => error.code === 'policy_version_mismatch')
  assert.equal(creates, 0)
  assert.deepEqual(rememberedSessions(target), [sessionId])
})
