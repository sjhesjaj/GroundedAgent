// Who is logged in. The role only picks the page: the server checks the token
// and the role again on every request. The token itself lives in localStorage
// (auth.ts); this store holds the account it belongs to.
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { authApi } from '@/api'
import { clearToken, getToken, setToken } from '@/auth'
import type { AuthUser } from '@/types'

export const SESSION_EXPIRED_NOTICE = '登录已失效或已过期，请重新登录。'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  /** The stored token has been checked once (GET /auth/me) since the page loaded. */
  const checked = ref(false)
  /** Shown on the login page: an expired login, or why the check failed. */
  const notice = ref('')
  let checking: Promise<AuthUser | null> | null = null

  /** The account of the stored token, asked once per page load. */
  function restore(): Promise<AuthUser | null> {
    if (checked.value) return Promise.resolve(user.value)
    checking ??= (async () => {
      try {
        if (getToken()) user.value = await authApi.me()
      } catch (error) {
        // A 401 already went through onUnauthorized (token dropped, notice set).
        // Anything else (the API is down) keeps the token for the next load.
        const failure = error as { status?: number, message?: string }
        if (failure.status !== 401) {
          user.value = null
          notice.value = failure.message ?? ''
        }
      } finally {
        checked.value = true
        checking = null
      }
      return user.value
    })()
    return checking
  }

  async function login(username: string, password: string): Promise<AuthUser> {
    const result = await authApi.login(username, password)
    setToken(result.access_token)
    user.value = result.user
    notice.value = ''
    checked.value = true
    return result.user
  }

  /** Drops the token; the page goes back to the login view (App.vue). */
  function logout(message = '') {
    clearToken()
    user.value = null
    notice.value = message
    checked.value = true
  }

  return { user, checked, notice, restore, login, logout }
})
