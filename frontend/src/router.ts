// Three pages: the login, the customer's service center and the operator's
// approval desk. Hash URLs (#/login, #/customer, #/operator) as before, so the
// static build needs no server-side routing.
import { createRouter, createWebHashHistory, type RouterHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import type { AuthUser, Role } from '@/types'
import LoginView from '@/views/LoginView.vue'

declare module 'vue-router' {
  interface RouteMeta {
    /** The only role that may open the page; a page without one is public. */
    role?: Role
  }
}

/** Where an account lands after login, and where a page of the other role sends it. */
export function homeOf(user: AuthUser) {
  return { name: user.role === 'operator' ? 'operator' : 'customer' }
}

export function createAppRouter(history: RouterHistory = createWebHashHistory()) {
  const router = createRouter({
    history,
    routes: [
      { path: '/', redirect: { name: 'login' } },
      { path: '/login', name: 'login', component: LoginView },
      { path: '/customer', name: 'customer', component: () => import('@/views/CustomerView.vue'),
        meta: { role: 'customer' } },
      { path: '/operator', name: 'operator', component: () => import('@/views/OperatorView.vue'),
        meta: { role: 'operator' } },
      { path: '/:unknown(.*)*', redirect: { name: 'login' } },
    ],
  })

  router.beforeEach(async (to) => {
    const user = await useAuthStore().restore()
    if (!user) return to.name === 'login' ? true : { name: 'login' }
    if (to.name === 'login') return homeOf(user)
    if (to.meta.role && to.meta.role !== user.role) return homeOf(user)
    return true
  })

  return router
}
