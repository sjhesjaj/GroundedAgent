import assert from 'node:assert/strict'
import test from 'node:test'
import { AftersalesApiError } from '../src/api.js'
import {
  SESSION_STORAGE_KEY,
  forgetSession,
  openSession,
  rememberSession,
  rememberedSessions,
} from '../src/sessionMemory.js'

const idA = 'a'.repeat(32)
const idB = 'b'.repeat(32)
const newId = 'c'.repeat(32)

function memoryStorage(initial) {
  const values = new Map(initial === undefined ? [] : [[SESSION_STORAGE_KEY, initial]])
  return {
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => { values.set(key, String(value)) },
    removeItem: (key) => { values.delete(key) },
  }
}

// A browser tab: its own sessionStorage, the origin's shared localStorage.
function tab(own, shared = memoryStorage()) {
  return { sessionStorage: memoryStorage(own), localStorage: shared }
}

function fakeApi(views = {}, created = { session_id: newId, status: 'OPEN' }) {
  const calls = { session: [], createSession: [] }
  return {
    calls,
    session: async (id) => {
      calls.session.push(id)
      if (!(id in views)) throw new AftersalesApiError('gone', 404, 'session_not_found')
      if (views[id] instanceof Error) throw views[id]
      return views[id]
    },
    createSession: async (personaId) => {
      calls.createSession.push(personaId)
      return created
    },
  }
}

test('a remembered session the server still has is restored, not replaced', async () => {
  const target = tab(undefined, memoryStorage(idA))
  const view = { session_id: idA, status: 'WAITING_APPROVAL', messages: [{ role: 'customer', text: '我要退货' }] }
  const api = fakeApi({ [idA]: view })

  assert.deepEqual(await openSession(api, 'demo-a', target), { data: view, restored: true })
  assert.deepEqual(api.calls, { session: [idA], createSession: [] })
  assert.deepEqual(rememberedSessions(target), [idA])
})

test('each tab reloads its own session; a new tab gets the last one', async () => {
  const shared = memoryStorage()
  const tabA = tab(undefined, shared)
  rememberSession(idA, tabA)                    // tab A opens session A
  const tabB = tab(undefined, shared)
  rememberSession(idB, tabB)                    // tab B opens session B: the shared value is now B
  const api = fakeApi({ [idA]: { session_id: idA }, [idB]: { session_id: idB } })

  assert.equal((await openSession(api, 'demo-a', tabA)).data.session_id, idA)
  assert.equal((await openSession(api, 'demo-a', tabB)).data.session_id, idB)
  assert.equal((await openSession(api, 'demo-a', tab(undefined, shared))).data.session_id, idB)
  assert.deepEqual(api.calls.createSession, [])
})

test('a remembered session the server no longer has (404) is forgotten and a new one created', async () => {
  const target = tab(idA, memoryStorage(idB))
  const api = fakeApi({})

  const opened = await openSession(api, 'demo-a', target)

  assert.deepEqual(opened, { data: { session_id: newId, status: 'OPEN' }, restored: false })
  assert.deepEqual(api.calls, { session: [idA, idB], createSession: ['demo-a'] })
  assert.deepEqual(rememberedSessions(target), [])
  rememberSession(newId, target)                // what App.vue's activate() does next
  assert.deepEqual(rememberedSessions(target), [newId])
})

test("a tab's lost session falls back to the browser's last session", async () => {
  const target = tab(idA, memoryStorage(idB))
  const api = fakeApi({ [idB]: { session_id: idB } })

  const opened = await openSession(api, 'demo-a', target)

  assert.deepEqual(opened, { data: { session_id: idB }, restored: true })
  assert.deepEqual(rememberedSessions(target), [idB])
})

test('without usable storage the page starts a new session and never throws', async () => {
  const blocked = Object.defineProperties({}, {
    localStorage: { get() { throw new DOMException('The operation is insecure.', 'SecurityError') } },
    sessionStorage: { get() { throw new DOMException('The operation is insecure.', 'SecurityError') } },
  })
  const throwing = {
    getItem() { throw new Error('denied') },
    setItem() { throw new DOMException('quota', 'QuotaExceededError') },
    removeItem() { throw new Error('denied') },
  }
  for (const target of [blocked, { localStorage: throwing, sessionStorage: throwing }, {}, null]) {
    const api = fakeApi({})
    const opened = await openSession(api, 'demo-a', target)
    assert.equal(opened.restored, false)
    assert.deepEqual(api.calls, { session: [], createSession: ['demo-a'] })
    assert.doesNotThrow(() => rememberSession(newId, target))
    assert.doesNotThrow(() => forgetSession(newId, target))
    assert.deepEqual(rememberedSessions(target), [])
  }
})

test('any other failure is reported and keeps the remembered session for the next load', async () => {
  for (const error of [new AftersalesApiError('down'),
    new AftersalesApiError('recovering', 409, 'recovery_pending'),
    new AftersalesApiError('broken', 500, 'agent_internal_error')]) {
    const target = tab(undefined, memoryStorage(idA))
    const api = fakeApi({ [idA]: error })
    await assert.rejects(openSession(api, 'demo-a', target), error)
    assert.deepEqual(api.calls.createSession, [])
    assert.deepEqual(rememberedSessions(target), [idA])
  }
})

test('a malformed remembered value is ignored', async () => {
  for (const value of ['', 'not-a-session', '../operator', 'A'.repeat(32), idA + 'x']) {
    const api = fakeApi({})
    const opened = await openSession(api, 'demo-a', tab(value, memoryStorage(value)))
    assert.equal(opened.restored, false)
    assert.deepEqual(api.calls.session, [])
  }
})
