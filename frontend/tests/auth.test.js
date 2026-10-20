import assert from 'node:assert/strict'
import test from 'node:test'
import { AftersalesApiError, aftersalesApi, authApi } from '../src/api.js'
import { TOKEN_STORAGE_KEY, clearToken, getToken, onUnauthorized, setToken } from '../src/auth.js'

function memoryStorage() {
  const values = new Map()
  return {
    values,
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => { values.set(key, String(value)) },
    removeItem: (key) => { values.delete(key) },
  }
}

// A browser-like localStorage for one test (Node has none by default).
function withStorage(t, storage = memoryStorage()) {
  Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true, writable: true })
  t.after(() => { delete globalThis.localStorage })
  return storage
}

function capture(t, response) {
  const seen = []
  t.mock.method(globalThis, 'fetch', async (url, options = {}) => {
    seen.push({ url, headers: new Headers(options.headers), body: options.body })
    return typeof response === 'function' ? response() : Response.json(response)
  })
  return seen
}

test('a stored token goes on every API request as a bearer header', async (t) => {
  withStorage(t)
  setToken('token-123')
  const seen = capture(t, { session_id: 'a'.repeat(32), status: 'OPEN' })
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

test('without a token no Authorization header is sent', async (t) => {
  withStorage(t)
  const seen = capture(t, { status: 'OPEN' })
  await aftersalesApi.demo()
  assert.equal(seen[0].headers.get('authorization'), null)
})

test('a 401 drops the token and sends the page back to the login view', async (t) => {
  const storage = withStorage(t)
  setToken('expired-token')
  let redirected = 0
  onUnauthorized(() => { redirected += 1 })
  t.after(() => onUnauthorized(null))
  capture(t, () => Response.json({ detail: { code: 'token_expired' } }, { status: 401 }))
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

test('a 403 is a typed error that keeps the login', async (t) => {
  withStorage(t)
  setToken('customer-token')
  let redirected = 0
  onUnauthorized(() => { redirected += 1 })
  t.after(() => onUnauthorized(null))
  capture(t, () => Response.json({ detail: { code: 'forbidden' } }, { status: 403 }))
  await assert.rejects(aftersalesApi.decide('a'.repeat(32), 'PA-DEMO', 'APPROVE'), (error) => {
    assert.equal(error.code, 'forbidden')
    assert.match(error.message, /无权/)
    return true
  })
  assert.equal(redirected, 0)
  assert.equal(getToken(), 'customer-token')
})

test('login sends only the credentials, no stored token, and a failed login is not a logout', async (t) => {
  withStorage(t)
  setToken('older-token')
  let redirected = 0
  onUnauthorized(() => { redirected += 1 })
  t.after(() => onUnauthorized(null))
  const seen = capture(t, () => Response.json({ detail: { code: 'invalid_credentials' } }, { status: 401 }))
  await assert.rejects(authApi.login('customer-a', 'wrong'), (error) => {
    assert.equal(error.code, 'invalid_credentials')
    assert.match(error.message, /用户名或密码错误/)
    return true
  })
  assert.equal(seen[0].url, '/auth/login')
  assert.equal(seen[0].headers.get('authorization'), null)
  assert.deepEqual(JSON.parse(seen[0].body), { username: 'customer-a', password: 'wrong' })
  assert.equal(redirected, 0)
})

test('logout clears only the token, and unavailable storage never throws', (t) => {
  const storage = withStorage(t)
  storage.setItem('groundedagent.aftersales.session', 'b'.repeat(32))
  setToken('token-123')
  clearToken()
  assert.equal(getToken(), '')
  assert.equal(storage.getItem('groundedagent.aftersales.session'), 'b'.repeat(32))
  const broken = { get localStorage() { throw new Error('blocked') } }
  assert.equal(getToken(broken), '')
  assert.doesNotThrow(() => setToken('x', broken))
  assert.doesNotThrow(() => clearToken(broken))
})
