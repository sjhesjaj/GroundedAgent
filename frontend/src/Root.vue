<script setup>
// Login, then the view of the account's role: customers get the chat
// (App.vue), operators the approval desk (OperatorView.vue). The role only
// picks the page; the server checks it again on every request. Any 401 (no,
// invalid or expired token) drops the token and comes back here.
import { onMounted, ref } from 'vue'
import { authApi } from './api'
import { clearToken, getToken, onUnauthorized, setToken } from './auth'
import App from './App.vue'
import LoginView from './components/LoginView.vue'
import OperatorView from './OperatorView.vue'

const user = ref(null)
const checking = ref(true)
const notice = ref('')

function showLogin(message = '') {
  clearToken()
  user.value = null
  notice.value = message
  history.replaceState(null, '', '#/login')
}

function enter(account) {
  user.value = account
  notice.value = ''
  history.replaceState(null, '', account.role === 'operator' ? '#/operator' : '#/customer')
}

onUnauthorized(() => showLogin('登录已失效或已过期，请重新登录。'))

function loggedIn({ access_token: token, user: account }) {
  setToken(token)
  enter(account)
}

onMounted(async () => {
  try {
    if (getToken()) enter(await authApi.me())
    else showLogin()
  } catch (error) {
    // 401 already went through onUnauthorized; keep the token on a network error.
    if (error.status !== 401) {
      user.value = null
      notice.value = error.message
    }
  } finally {
    checking.value = false
  }
})
</script>

<template>
  <LoginView v-if="!user" :checking="checking" :notice="notice" @logged-in="loggedIn" />
  <OperatorView v-else-if="user.role === 'operator'" :user="user" @logout="showLogin()" />
  <App v-else :user="user" @logout="showLogin()" />
</template>
