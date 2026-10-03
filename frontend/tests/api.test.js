import assert from 'node:assert/strict'
import test from 'node:test'
import { AftersalesApiError, aftersalesApi } from '../src/api.js'

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

const requests = [
  ['demo', () => aftersalesApi.demo(untrustedExtras), 'GET', '/demo'],
  ['resetDemo', () => aftersalesApi.resetDemo(untrustedExtras), 'POST', '/demo/reset'],
  ['createSession', () => aftersalesApi.createSession('demo-a', untrustedExtras), 'POST', '/sessions', { persona_id: 'demo-a' }],
  ['session', () => aftersalesApi.session(sessionId, untrustedExtras), 'GET', `/sessions/${sessionId}`],
  ['sendMessage', () => aftersalesApi.sendMessage(sessionId, '我要退货', untrustedExtras), 'POST', `/sessions/${sessionId}/messages`, { text: '我要退货' }],
  ['approve', () => aftersalesApi.decide(sessionId, pendingId, 'APPROVE', untrustedExtras), 'POST', `/operator/sessions/${sessionId}/decision`, { pending_action_id: pendingId, decision: 'APPROVE' }],
  ['reject', () => aftersalesApi.decide(sessionId, pendingId, 'REJECT', untrustedExtras), 'POST', `/operator/sessions/${sessionId}/decision`, { pending_action_id: pendingId, decision: 'REJECT' }],
]

for (const [name, call, method, path, body] of requests) {
  test(`${name} uses only the documented endpoint and request fields`, async (t) => {
    const payload = { session_id: sessionId, status: 'OPEN' }
    const fetchMock = t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
      assert.equal(url, `/api/aftersales${path}`)
      assert.equal((options.method || 'GET').toUpperCase(), method)
      if (body === undefined) {
        assert.equal(options.body, undefined, 'GET and reset must not send a request body')
      } else {
        assert.equal(new Headers(options.headers).get('content-type'), 'application/json')
        assert.deepEqual(JSON.parse(options.body), body, 'trusted identity and approval flags must never enter request bodies')
      }
      return Response.json(payload)
    })

    assert.deepEqual(await call(), payload)
    assert.equal(fetchMock.mock.callCount(), 1)
  })
}

test('session identifiers remain a single encoded path segment', async (t) => {
  const id = '../operator/other?role=admin#fragment'
  const urls = []
  t.mock.method(globalThis, 'fetch', async (url) => {
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

const errors = [
  [404, { code: 'session_not_found' }, 'session_not_found', /会话.*失效/],
  [422, [{ loc: ['body', 'text'], msg: 'raw validation diagnostics' }], 'http_422', /校验/],
  [503, { code: 'llm_unavailable' }, 'llm_unavailable', /模型服务.*不可用/],
  [500, { code: 'agent_internal_error' }, 'agent_internal_error', /处理失败/],
]

for (const [status, detail, code, message] of errors) {
  test(`HTTP ${status} is a typed, recoverable error with a public notice`, async (t) => {
    t.mock.method(globalThis, 'fetch', async () => Response.json({ detail }, { status }))

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

test('network failures do not claim that a message or decision succeeded', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => {
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
  test(`successful HTTP with ${name} cannot masquerade as an API result`, async (t) => {
    t.mock.method(globalThis, 'fetch', async () => new Response(body, { status: 200 }))

    await assert.rejects(aftersalesApi.demo(), (error) => {
      assert.ok(error instanceof AftersalesApiError)
      assert.equal(error.status, 200)
      assert.equal(error.code, 'invalid_response')
      return true
    })
  })
}

test('malformed HTTP error bodies preserve the status without exposing raw content', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response('<html>private upstream diagnostic</html>', { status: 503 }))

  await assert.rejects(aftersalesApi.demo(), (error) => {
    assert.ok(error instanceof AftersalesApiError)
    assert.equal(error.status, 503)
    assert.equal(error.code, 'http_503')
    assert.doesNotMatch(error.message, /private upstream diagnostic|<html>/)
    return true
  })
})
