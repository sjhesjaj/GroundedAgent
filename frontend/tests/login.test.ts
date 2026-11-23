// @vitest-environment jsdom
//
// Login, the role's landing page and the way back: the real router guard, auth
// store, login page and 401 hook (src/app.ts), with fetch mocked and an
// in-memory history.
import assert from 'node:assert/strict'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, onTestFinished, test, vi } from 'vitest'
import { createMemoryHistory } from 'vue-router'

import { createAftersalesApp } from '../src/app'
import { TOKEN_STORAGE_KEY } from '../src/auth'
import { SESSION_EXPIRED_NOTICE } from '../src/stores/auth'
import type { AuthUser } from '../src/types'
import LoginView from '../src/views/LoginView.vue'

const CUSTOMER: AuthUser = { username: 'customer-a', role: 'customer', display_name: '演示顾客甲', persona_id: 'demo-a' }
const OPERATOR: AuthUser = { username: 'operator', role: 'operator', display_name: '售后操作员 op-demo-1', persona_id: null }

type Route = (body: unknown) => Response | Promise<Response>

/** fetch answering `METHOD url` from a table; anything else fails the test. */
function backend(routes: Record<string, Route>) {
  const seen: { call: string, body: unknown }[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url, options = {}) => {
    const call = `${(options.method || 'GET').toUpperCase()} ${url}`
    const body = options.body === undefined ? undefined : JSON.parse(String(options.body))
    seen.push({ call, body })
    const route = routes[call]
    if (!route) throw new Error(`unexpected request ${call}`)
    return route(body)
  })
  return seen
}

async function settle() {
  for (let round = 0; round < 10; round += 1) await flushPromises()
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
})

/** The app as main.ts builds it, at `path`, with the login page mounted (not the role pages). */
async function loginPage(path = '/login') {
  const built = createAftersalesApp(createMemoryHistory())
  await built.router.push(path)
  const wrapper = mount(LoginView, { global: { plugins: [built.pinia, built.router] }, attachTo: document.body })
  onTestFinished(() => wrapper.unmount())
  return { ...built, wrapper }
}

const submitButton = (wrapper: ReturnType<typeof mount>) => wrapper.get('button.submit')

// -- logging in ----------------------------------------------------------------

for (const [account, landing] of [[CUSTOMER, 'customer'], [OPERATOR, 'operator']] as const) {
  test(`${account.username} logs in and lands on the ${landing} page`, async () => {
    const seen = backend({
      'POST /auth/login': () => Response.json({ access_token: `token-${account.username}`, token_type: 'bearer',
        expires_in: 7200, user: account }),
    })
    const { wrapper, router, auth } = await loginPage()

    await wrapper.get('#login-username').setValue(`  ${account.username} `)
    await wrapper.get('#login-password').setValue('secret')
    await wrapper.get('form').trigger('submit')
    // The landing page is loaded on demand: wait for the navigation, not a fixed number of ticks.
    await vi.waitFor(() => assert.equal(router.currentRoute.value.name, landing))

    assert.deepEqual(seen, [{ call: 'POST /auth/login', body: { username: account.username, password: 'secret' } }])
    assert.equal(localStorage.getItem(TOKEN_STORAGE_KEY), `token-${account.username}`)
    assert.deepEqual(auth.user, account)
  })
}

test('a failed login shows why and stays on the login page', async () => {
  backend({ 'POST /auth/login': () => Response.json({ detail: { code: 'invalid_credentials' } }, { status: 401 }) })
  const { wrapper, router } = await loginPage()

  await wrapper.get('#login-username').setValue('customer-a')
  await wrapper.get('#login-password').setValue('wrong')
  await wrapper.get('form').trigger('submit')
  await settle()

  assert.equal(router.currentRoute.value.name, 'login')
  assert.match(wrapper.get('[role="alert"]').text(), /用户名或密码错误/)
  assert.equal(localStorage.getItem(TOKEN_STORAGE_KEY), null)
  assert.equal((wrapper.get('#login-password').element as HTMLInputElement).value, 'wrong', 'kept for a retry')
})

test('the login button stays disabled until both fields are filled', async () => {
  backend({})
  const { wrapper } = await loginPage()
  assert.equal(submitButton(wrapper).attributes('disabled'), '')
  await wrapper.get('#login-username').setValue('   ')
  await wrapper.get('#login-password').setValue('x')
  assert.equal(submitButton(wrapper).attributes('disabled'), '', 'a blank username is not a username')
  await wrapper.get('#login-username').setValue('operator')
  assert.equal(submitButton(wrapper).attributes('disabled'), undefined)
})

test('a demo account fills the form without logging in', async () => {
  const seen = backend({})
  const { wrapper, router } = await loginPage()
  const accounts = wrapper.findAll('button.demo-account')
  assert.deepEqual(accounts.map((button) => button.get('strong').text()), ['演示顾客甲', '演示顾客乙', '售后操作员'])

  await accounts[2].trigger('click')

  assert.equal((wrapper.get('#login-username').element as HTMLInputElement).value, 'operator')
  assert.equal((wrapper.get('#login-password').element as HTMLInputElement).value, 'operator-2026')
  assert.equal(seen.length, 0)
  assert.equal(router.currentRoute.value.name, 'login')
})

test('a production build shows no demo accounts unless VITE_DEMO_ACCOUNTS is true', async () => {
  backend({})
  vi.stubEnv('DEV', false)
  const hidden = await loginPage()
  assert.equal(hidden.wrapper.findAll('button.demo-account').length, 0)
  vi.stubEnv('VITE_DEMO_ACCOUNTS', 'true')
  const shown = await loginPage()
  assert.equal(shown.wrapper.findAll('button.demo-account').length, 3)
})

// -- the router guard ------------------------------------------------------------

test('without a login every page leads to the login page', async () => {
  const seen = backend({})
  const { router } = createAftersalesApp(createMemoryHistory())
  for (const path of ['/', '/customer', '/operator', '/no-such-page']) {
    await router.push(path)
    assert.equal(router.currentRoute.value.name, 'login', path)
  }
  assert.equal(seen.length, 0, 'no stored token: nothing to check')
})

for (const [account, home, other] of [[CUSTOMER, 'customer', '/operator'], [OPERATOR, 'operator', '/customer']] as const) {
  test(`a stored ${account.role} login opens its own page and is kept off the other role's`, async () => {
    localStorage.setItem(TOKEN_STORAGE_KEY, 'stored-token')
    const seen = backend({ 'GET /auth/me': () => Response.json(account) })
    const { router } = createAftersalesApp(createMemoryHistory())
    for (const path of ['/', '/login', other, `/${home}`]) {
      await router.push(path)
      assert.equal(router.currentRoute.value.name, home, path)
    }
    assert.deepEqual(seen.map((request) => request.call), ['GET /auth/me'], 'the token is checked once per page load')
  })
}

test('an expired stored login goes back to the login page with a notice', async () => {
  localStorage.setItem(TOKEN_STORAGE_KEY, 'expired-token')
  backend({ 'GET /auth/me': () => Response.json({ detail: { code: 'token_expired' } }, { status: 401 }) })
  const { wrapper, router, auth } = await loginPage('/customer')
  await settle()

  assert.equal(router.currentRoute.value.name, 'login')
  assert.equal(auth.notice, SESSION_EXPIRED_NOTICE)
  assert.match(wrapper.get('[role="alert"]').text(), /登录已失效或已过期/)
  assert.equal(localStorage.getItem(TOKEN_STORAGE_KEY), null)
})

test('a network failure while checking the stored login keeps the token for the next load', async () => {
  localStorage.setItem(TOKEN_STORAGE_KEY, 'stored-token')
  vi.spyOn(globalThis, 'fetch').mockImplementation(async () => { throw new TypeError('offline') })
  const { router, auth } = createAftersalesApp(createMemoryHistory())
  await router.push('/operator')

  assert.equal(router.currentRoute.value.name, 'login')
  assert.match(auth.notice, /连接中断/)
  assert.equal(localStorage.getItem(TOKEN_STORAGE_KEY), 'stored-token')
})

// -- leaving a page --------------------------------------------------------------

test('退出登录 in the account menu of a page returns to the login page', async () => {
  localStorage.setItem(TOKEN_STORAGE_KEY, 'operator-token')
  backend({
    'GET /auth/me': () => Response.json(OPERATOR),
    'GET /api/aftersales/operator/sessions': () => Response.json({ items: [] }),
  })
  const { app, router } = createAftersalesApp(createMemoryHistory())
  await router.push('/operator')
  const root = document.createElement('div')
  document.body.append(root)
  app.mount(root)
  onTestFinished(() => { app.unmount(); root.remove() })
  await settle()

  assert.equal(router.currentRoute.value.name, 'operator')
  root.querySelector<HTMLButtonElement>('button.account')!.click()
  await settle()
  const logout = [...document.body.querySelectorAll<HTMLElement>('.el-dropdown-menu__item')]
    .find((item) => item.textContent?.trim() === '退出登录')
  assert.ok(logout, 'the account menu offers 退出登录')
  logout.click()
  await settle()

  assert.equal(router.currentRoute.value.name, 'login')
  assert.equal(localStorage.getItem(TOKEN_STORAGE_KEY), null)
  assert.ok(root.querySelector('#login-username'), 'the login form is shown')
})
