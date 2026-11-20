import assert from 'node:assert/strict'
import { test, vi } from 'vitest'
import { AftersalesApiError, aftersalesApi } from '../src/api'

const sessionId = 'a'.repeat(32)
const pendingId = 'PA-DEMO'
const untrustedExtras = {
  customer_id: 'customer-forged',
  client_id: 'client-forged',
  approver_ref: 'operator-forged',
  role: 'admin',
  skip_approval: true,
  approved: true,
}

// Each call also passes untrusted extras, as a caller that ignores the types
// could: none of them may reach the URL or the body.
type Untyped = (...args: unknown[]) => Promise<unknown>
const api = aftersalesApi as unknown as Record<keyof typeof aftersalesApi, Untyped>

const requests: [string, () => Promise<unknown>, string, string, unknown?][] = [
  ['demo', () => api.demo(untrustedExtras), 'GET', '/demo'],
  ['resetDemo', () => api.resetDemo(untrustedExtras), 'POST', '/demo/reset'],
  ['createSession', () => api.createSession(untrustedExtras), 'POST', '/sessions'],
  ['session', () => api.session(sessionId, untrustedExtras), 'GET', `/sessions/${sessionId}`],
  ['sendMessage', () => api.sendMessage(sessionId, '我要退货', untrustedExtras), 'POST', `/sessions/${sessionId}/messages`, { text: '我要退货' }],
  ['approve', () => api.decide(sessionId, pendingId, 'APPROVE', untrustedExtras), 'POST', `/operator/sessions/${sessionId}/decision`, { pending_action_id: pendingId, decision: 'APPROVE' }],
  ['reject', () => api.decide(sessionId, pendingId, 'REJECT', untrustedExtras), 'POST', `/operator/sessions/${sessionId}/decision`, { pending_action_id: pendingId, decision: 'REJECT' }],
  // The read-only order views: the customer is the token's, never a parameter.
  ['orders', () => api.orders(untrustedExtras), 'GET', '/orders'],
  ['orderSummary', () => api.orderSummary(sessionId, pendingId, untrustedExtras), 'GET', `/operator/sessions/${sessionId}/actions/${pendingId}/order-summary`],
]

for (const [name, call, method, path, body] of requests) {
  test(`${name} uses only the documented endpoint and request fields`, async () => {
    const payload = { session_id: sessionId, status: 'OPEN' }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options = {}) => {
      assert.equal(url, `/api/aftersales${path}`)
      assert.equal((options.method || 'GET').toUpperCase(), method)
      if (body === undefined) {
        assert.equal(options.body, undefined, 'GET, reset and session creation send no request body')
      } else {
        assert.equal(new Headers(options.headers).get('content-type'), 'application/json')
        assert.deepEqual(JSON.parse(String(options.body)), body, 'trusted identity and approval flags must never enter request bodies')
      }
      return Response.json(payload)
    })

    assert.deepEqual(await call(), payload)
    assert.equal(fetchMock.mock.calls.length, 1)
  })
}

test('session identifiers remain a single encoded path segment', async () => {
  const id = '../operator/other?role=admin#fragment'
  const urls: unknown[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
    urls.push(url)
    return Response.json({ status: 'OPEN' })
  })

  await aftersalesApi.session(id)
  await aftersalesApi.sendMessage(id, '订单状态')
  await aftersalesApi.decide(id, pendingId, 'REJECT')

  const encoded = encodeURIComponent(id)
  assert.deepEqual(urls, [
    `/api/aftersales/sessions/${encoded}`,
    `/api/aftersales/sessions/${encoded}/messages`,
    `/api/aftersales/operator/sessions/${encoded}/decision`,
  ])
})

const errors: [number, unknown, string, RegExp][] = [
  [404, { code: 'session_not_found' }, 'session_not_found', /会话.*失效/],
  [422, [{ loc: ['body', 'text'], msg: 'raw validation diagnostics' }], 'http_422', /校验/],
  [503, { code: 'llm_unavailable' }, 'llm_unavailable', /模型服务.*不可用/],
  [500, { code: 'agent_internal_error' }, 'agent_internal_error', /处理失败/],
]

for (const [status, detail, code, message] of errors) {
  test(`HTTP ${status} is a typed, recoverable error with a public notice`, async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => Response.json({ detail }, { status }))

    await assert.rejects(aftersalesApi.sendMessage(sessionId, '我要退货'), (error) => {
      assert.ok(error instanceof AftersalesApiError)
      assert.equal(error.status, status)
      assert.equal(error.code, code)
      assert.match(error.message, message)
      assert.doesNotMatch(error.message, /raw validation diagnostics/)
      return true
    })
  })
}

test('network failures do not claim that a message or decision succeeded', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
    throw new TypeError('private transport diagnostic')
  })

  for (const call of [
    () => aftersalesApi.sendMessage(sessionId, '我要退货'),
    () => aftersalesApi.decide(sessionId, pendingId, 'APPROVE'),
  ]) {
    await assert.rejects(call(), (error) => {
      assert.ok(error instanceof AftersalesApiError)
      assert.equal(error.status, 0)
      assert.equal(error.code, 'network_error')
      assert.match(error.message, /无法确认请求结果/)
      assert.doesNotMatch(error.message, /private transport diagnostic/)
      return true
    })
  }
})

for (const [name, body] of [
  ['invalid JSON', '<html>upstream failure</html>'],
  ['null', 'null'],
  ['string', '"unexpected"'],
  ['number', '42'],
  ['boolean', 'false'],
  ['array', '[]'],
]) {
  test(`successful HTTP with ${name} cannot masquerade as an API result`, async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(body, { status: 200 }))

    await assert.rejects(aftersalesApi.demo(), (error) => {
      assert.ok(error instanceof AftersalesApiError)
      assert.equal(error.status, 200)
      assert.equal(error.code, 'invalid_response')
      return true
    })
  })
}

test('malformed HTTP error bodies preserve the status without exposing raw content', async () => {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response('<html>private upstream diagnostic</html>', { status: 503 }))

  await assert.rejects(aftersalesApi.demo(), (error) => {
    assert.ok(error instanceof AftersalesApiError)
    assert.equal(error.status, 503)
    assert.equal(error.code, 'http_503')
    assert.doesNotMatch(error.message, /private upstream diagnostic|<html>/)
    return true
  })
})
