// The browser remembers its current session id across page reloads; the server
// keeps sessions across restarts (M2). The id goes to localStorage (the
// browser's last session, for a new tab) and to sessionStorage (this tab's own
// session: two tabs that each reload get their own session back, although
// localStorage is shared). Storage can be unavailable (private mode, blocked
// site data, a full quota): every access is guarded, and the page then starts a
// new session on load, as it always did.

export const SESSION_STORAGE_KEY = 'groundedagent.aftersales.session'
const STORES = ['sessionStorage', 'localStorage']   // this tab's session first
const SESSION_ID = /^[0-9a-f]{32}$/

function storageOf(target, name) {
  try {
    return target?.[name] ?? null   // the getter itself may throw
  } catch {
    return null
  }
}

export function rememberSession(sessionId, target = globalThis) {
  for (const name of STORES) {
    try {
      storageOf(target, name)?.setItem(SESSION_STORAGE_KEY, sessionId)
    } catch {
      // Not remembered there.
    }
  }
}

// The remembered session ids, this tab's first; valid ids only, each once.
export function rememberedSessions(target = globalThis) {
  const found = []
  for (const name of STORES) {
    try {
      const value = storageOf(target, name)?.getItem(SESSION_STORAGE_KEY)
      if (typeof value === 'string' && SESSION_ID.test(value) && !found.includes(value)) found.push(value)
    } catch {
      // Nothing readable there.
    }
  }
  return found
}

export function forgetSession(sessionId, target = globalThis) {
  for (const name of STORES) {
    try {
      const storage = storageOf(target, name)
      if (storage?.getItem(SESSION_STORAGE_KEY) === sessionId) storage.removeItem(SESSION_STORAGE_KEY)
    } catch {
      // Nothing to forget there.
    }
  }
}

// The demo customer to use: the preferred one if the demo still offers it,
// else the first. The empty string when there is none.
export function choosePersona(demo, preferredPersonaId) {
  const personas = Array.isArray(demo?.personas) ? demo.personas : []
  if (personas.some((persona) => persona.persona_id === preferredPersonaId)) return preferredPersonaId
  return personas[0]?.persona_id || ''
}

// Page start-up and "重新连接": the demo, then the remembered session (or a
// new one for the chosen customer). A failure leaves every remembered id as it
// was, so the next attempt goes back to the same session.
export async function connectPage(api, preferredPersonaId, target = globalThis) {
  const demo = await api.demo()
  const personaId = choosePersona(demo, preferredPersonaId)
  if (!personaId) throw new Error('Demo 暂无可用客户，请检查后端服务。')
  return { demo, ...(await openSession(api, personaId, target)) }
}

// The recovery the notice bar offers. "新建会话" only when the server said the
// session is gone (404); a failed page load, 409 recovery_pending or a 5xx
// offer "重新连接", which returns to the remembered session.
export function recoveryAction({ hasSession, invalidSession, syncRequired }) {
  if (invalidSession) return 'new_session'
  if (syncRequired) return 'refresh'
  if (!hasSession) return 'reconnect'
  return null
}

// The session to show on page load: a remembered one while the server still
// has it, otherwise a new one. Only "no such session" (404) falls through; any
// other failure (network, 409 recovery_pending, 5xx) is the caller's to show,
// and the remembered id is kept for the next load.
export async function openSession(api, personaId, target = globalThis) {
  for (const remembered of rememberedSessions(target)) {
    try {
      return { data: await api.session(remembered), restored: true }
    } catch (error) {
      if (error?.status !== 404 && error?.code !== 'session_not_found') throw error
      forgetSession(remembered, target)
    }
  }
  return { data: await api.createSession(personaId), restored: false }
}
