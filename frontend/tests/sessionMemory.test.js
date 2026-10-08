import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { AftersalesApiError } from '../src/api.js'
import {
  SESSION_STORAGE_KEY,
  choosePersona,
  connectPage,
  forgetSession,
  openSession,
  recoveryAction,
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

// -- connecting the page: start-up and "重新连接" ------------------------------

const DEMO = { personas: [{ persona_id: 'demo-a' }, { persona_id: 'demo-b' }] }

// A backend that can be down, then up again.
function flakyBackend(views) {
  const state = { up: false, calls: { demo: 0, session: [], createSession: [] } }
  const api = {
    demo: async () => {
      state.calls.demo += 1
      if (!state.up) throw new AftersalesApiError('连接中断，暂时无法确认请求结果。')
      return DEMO
    },
    session: async (id) => {
      state.calls.session.push(id)
      if (!state.up) throw new AftersalesApiError('连接中断，暂时无法确认请求结果。')
      if (views[id] instanceof Error) throw views[id]
      if (!(id in views)) throw new AftersalesApiError('gone', 404, 'session_not_found')
      return views[id]
    },
    createSession: async (personaId) => {
      state.calls.createSession.push(personaId)
      return { session_id: newId, persona: { persona_id: personaId }, status: 'OPEN' }
    },
  }
  return { api, state }
}

test('reconnecting after a failed load returns to the remembered session, identity and storage unchanged', async () => {
  const sessionB = { session_id: idB, persona: { persona_id: 'demo-b' }, status: 'WAITING_APPROVAL', messages: [{}, {}, {}, {}] }
  const { api, state } = flakyBackend({ [idB]: sessionB })
  const target = tab(idB, memoryStorage(idB))

  // The page loads while the backend is down: an error, no session, nothing forgotten.
  await assert.rejects(connectPage(api, 'demo-a', target), (error) => error.code === 'network_error')
  assert.equal(recoveryAction({ hasSession: false, invalidSession: false, syncRequired: false }), 'reconnect')
  assert.deepEqual(rememberedSessions(target), [idB])

  // The backend is back; "重新连接" connects the page again.
  state.up = true
  const connected = await connectPage(api, 'demo-a', target)
  assert.equal(connected.restored, true)
  assert.equal(connected.data, sessionB)
  assert.equal(connected.data.persona.persona_id, 'demo-b')   // not the page's default customer
  assert.deepEqual(state.calls.createSession, [])
  rememberSession(connected.data.session_id, target)          // activate()
  assert.deepEqual(rememberedSessions(target), [idB])
})

test('reconnecting when the server no longer has the session (404) starts a new one', async () => {
  const { api, state } = flakyBackend({})
  state.up = true
  const target = tab(idB, memoryStorage(idB))

  const connected = await connectPage(api, 'demo-b', target)

  assert.equal(connected.restored, false)
  assert.deepEqual(state.calls.createSession, ['demo-b'])
  assert.deepEqual(rememberedSessions(target), [])
  rememberSession(connected.data.session_id, target)
  assert.deepEqual(rememberedSessions(target), [newId])
})

test('a session still recovering (409) is not replaced on reconnect', async () => {
  const { api, state } = flakyBackend({ [idB]: new AftersalesApiError('recovering', 409, 'recovery_pending') })
  state.up = true
  const target = tab(idB, memoryStorage(idB))
  await assert.rejects(connectPage(api, 'demo-a', target), (error) => error.code === 'recovery_pending')
  assert.deepEqual(state.calls.createSession, [])
  assert.deepEqual(rememberedSessions(target), [idB])
})

test('the demo customer falls back to the first one the demo offers', () => {
  assert.equal(choosePersona(DEMO, 'demo-b'), 'demo-b')
  assert.equal(choosePersona(DEMO, 'demo-x'), 'demo-a')
  assert.equal(choosePersona({ personas: [] }, 'demo-a'), '')
  assert.equal(choosePersona(null, 'demo-a'), '')
})

test('"新建会话" is offered only for a session the server no longer has', () => {
  const cases = [
    [{ hasSession: false, invalidSession: false, syncRequired: false }, 'reconnect'],   // failed load, 409, 5xx
    [{ hasSession: true, invalidSession: true, syncRequired: false }, 'new_session'],   // 404
    [{ hasSession: true, invalidSession: true, syncRequired: true }, 'new_session'],
    [{ hasSession: true, invalidSession: false, syncRequired: true }, 'refresh'],
    [{ hasSession: true, invalidSession: false, syncRequired: false }, null],
  ]
  for (const [state, expected] of cases) assert.equal(recoveryAction(state), expected, JSON.stringify(state))
})

test('App.vue wires start-up and "重新连接" to connect, never to newSession', () => {
  const source = readFileSync(new URL('../src/App.vue', import.meta.url), 'utf8')
  assert.match(source, /onMounted\(connect\)/)
  const buttons = [...source.matchAll(/<button\b[^>]*>[^<]*<\/button>/g)].map((match) => match[0])
  const reconnect = buttons.filter((button) => button.includes('重新连接'))
  assert.equal(reconnect.length, 1)
  assert.match(reconnect[0], /recovery === 'reconnect'/)
  assert.match(reconnect[0], /@click="connect"/)
  const recoveryNew = buttons.filter((button) => button.includes("recovery === 'new_session'"))
  assert.equal(recoveryNew.length, 1)
  assert.match(recoveryNew[0], /@click="newSession\(\)"/)
})
