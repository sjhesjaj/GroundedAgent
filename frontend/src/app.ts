// The application, assembled: Pinia, the router and the 401 hook. main.ts
// mounts it; tests build it with an in-memory history.
import { createPinia } from 'pinia'
import { createApp } from 'vue'
import { createWebHashHistory, type RouterHistory } from 'vue-router'

import App from '@/App.vue'
import { onUnauthorized } from '@/auth'
import { createAppRouter } from '@/router'
import { SESSION_EXPIRED_NOTICE, useAuthStore } from '@/stores/auth'

export function createAftersalesApp(history: RouterHistory = createWebHashHistory()) {
  const app = createApp(App)
  const pinia = createPinia()
  app.use(pinia)

  // Any 401 from an authenticated call (api.ts): the token is already dropped;
  // the store forgets the account and App.vue goes back to the login page.
  const auth = useAuthStore(pinia)
  onUnauthorized(() => auth.logout(SESSION_EXPIRED_NOTICE))

  const router = createAppRouter(history)
  app.use(router)
  return { app, pinia, router, auth }
}
