// The access token from POST /auth/login lives in localStorage and goes on
// every API request as `Authorization: Bearer <token>` (api.js). Logout only
// drops it: this version has no refresh token and no server-side revocation, so
// the token itself stays valid until it expires (2 hours). Storage can be
// unavailable (private mode, blocked site data); every access is guarded and
// the page then simply asks for a login again.

export const TOKEN_STORAGE_KEY = 'groundedagent.auth.token'

function storageOf(target) {
  try {
    return target?.localStorage ?? null   // the getter itself may throw
  } catch {
    return null
  }
}

export function getToken(target = globalThis) {
  try {
    const value = storageOf(target)?.getItem(TOKEN_STORAGE_KEY)
    return typeof value === 'string' ? value : ''
  } catch {
    return ''
  }
}

export function setToken(token, target = globalThis) {
  try {
    storageOf(target)?.setItem(TOKEN_STORAGE_KEY, token)
  } catch {
    // Not remembered: the next page load asks for a login again.
  }
}

export function clearToken(target = globalThis) {
  try {
    storageOf(target)?.removeItem(TOKEN_STORAGE_KEY)
  } catch {
    // Nothing to clear there.
  }
}

// Root.vue registers the "back to the login page" handler; api.js calls it on
// any 401 from an authenticated request.
let unauthorizedHandler = null

export function onUnauthorized(handler) {
  unauthorizedHandler = handler
}

export function notifyUnauthorized() {
  clearToken()
  unauthorizedHandler?.()
}
