import assert from 'node:assert/strict'
import { onTestFinished, test, vi } from 'vitest'
import { AftersalesApiError, aftersalesApi, authApi } from '../src/api'
import { TOKEN_STORAGE_KEY, clearToken, getToken, onUnauthorized, setToken } from '../src/auth'

function memoryStorage() {
  const values = new Map<string, string>()
  return {
    values,
    getItem: (key: string) => (values.has(key) ? values.get(key)! : null),
    setItem: (key: string, value: unknown) => { values.set(key, String(value)) },
    removeItem: (key: string) => { values.delete(key) },
  }
}

// A browser-like localStorage for one test (Node has none by default).
function withStorage(storage = memoryStorage()) {
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true, writable: true })
  onTestFinished(() => { delete (globalThis as { localStorage?: unknown }).localStorage })
  return storage
}

function capture(response: object | (() => Response)) {
  const seen: { url: unknown, headers: Headers, body: unknown }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options = {}) => {
    seen.push({ url, headers: new Headers(options.headers), body: options.body })
    return typeof response === 'function' ? response() : Response.json(response)
  })
  return seen
}

test('a stored token goes on every API request as a bearer header', async () => {
  withStorage()
  setToken('token-123')
  const seen = capture({ session_id: 'a'.repeat(32), status: 'OPEN' })
  await aftersalesApi.demo()
  await aftersalesApi.createSession()
  await aftersalesApi.sendMessage('a'.repeat(32), '你好')
  await aftersalesApi.operatorSessions()
  await authApi.me()
  assert.deepEqual(seen.map((request) => request.headers.get('authorization')), Array(5).fill('Bearer token-123'))
  assert.equal(seen[1].url, '/api/aftersales/sessions')
  assert.equal(seen[1].body, undefined, 'the customer comes from the token, not from a body')
  assert.equal(seen[3].url, '/api/aftersales/operator/sessions')
})

test('without a token no Authorization header is sent', async () => {
  withStorage()
  const seen = capture({ status: 'OPEN' })
  await aftersalesApi.demo()
  assert.equal(seen[0].headers.get('authorization'), null)
})

test('a 401 drops the token and sends the page back to the login view', async () => {
  const storage = withStorage()
  setToken('expired-token')
  let redirected = 0
  onUnauthorized(() => { redirected += 1 })
  onTestFinished(() => onUnauthorized(null))
  capture(() => Response.json({ detail: { code: 'token_expired' } }, { status: 401 }))
  await assert.rejects(aftersalesApi.session('a'.repeat(32)), (error) => {
    assert.ok(error instanceof AftersalesApiError)
    assert.equal(error.status, 401)
    assert.equal(error.code, 'token_expired')
    assert.match(error.message, /登录已过期/)
    return true
  })
  assert.equal(redirected, 1)
  assert.equal(storage.values.has(TOKEN_STORAGE_KEY), false)
})

test('a 403 is a typed error that keeps the login', async () => {
  withStorage()
  setToken('customer-token')
  let redirected = 0
  onUnauthorized(() => { redirected += 1 })
  onTestFinished(() => onUnauthorized(null))
  capture(() => Response.json({ detail: { code: 'forbidden' } }, { status: 403 }))
  await assert.rejects(aftersalesApi.decide('a'.repeat(32), 'PA-DEMO', 'APPROVE'), (error: AftersalesApiError) => {
    assert.equal(error.code, 'forbidden')
    assert.match(error.message, /无权/)
    return true
  })
  assert.equal(redirected, 0)
  assert.equal(getToken(), 'customer-token')
})

test('login sends only the credentials, no stored token, and a failed login is not a logout', async () => {
  withStorage()
  setToken('older-token')
  let redirected = 0
  onUnauthorized(() => { redirected += 1 })
  onTestFinished(() => onUnauthorized(null))
  const seen = capture(() => Response.json({ detail: { code: 'invalid_credentials' } }, { status: 401 }))
  await assert.rejects(authApi.login('customer-a', 'wrong'), (error: AftersalesApiError) => {
    assert.equal(error.code, 'invalid_credentials')
    assert.match(error.message, /用户名或密码错误/)
    return true
  })
  assert.equal(seen[0].url, '/auth/login')
  assert.equal(seen[0].headers.get('authorization'), null)
  assert.deepEqual(JSON.parse(String(seen[0].body)), { username: 'customer-a', password: 'wrong' })
  assert.equal(redirected, 0)
})

test('logout clears only the token, and unavailable storage never throws', () => {
  const storage = withStorage()
  storage.setItem('groundedagent.aftersales.session', 'b'.repeat(32))
  setToken('token-123')
  clearToken()
  assert.equal(getToken(), '')
  assert.equal(storage.getItem('groundedagent.aftersales.session'), 'b'.repeat(32))
  const broken = { get localStorage(): Storage { throw new Error('blocked') } }
  assert.equal(getToken(broken), '')
  assert.doesNotThrow(() => setToken('x', broken))
  assert.doesNotThrow(() => clearToken(broken))
})
