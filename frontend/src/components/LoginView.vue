<script setup>
import { ref } from 'vue'
import { authApi } from '../api'

defineProps({
  checking: { type: Boolean, default: false },
  notice: { type: String, default: '' },
})
const emit = defineEmits(['logged-in'])

const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')

async function submit() {
  if (busy.value || !username.value.trim() || !password.value) return
  busy.value = true
  error.value = ''
  try {
    emit('logged-in', await authApi.login(username.value.trim(), password.value))
    password.value = ''
  } catch (failure) {
    error.value = failure.message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login-shell">
    <form class="login-card panel" @submit.prevent="submit">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">G<span></span></span>
        <span><strong>GroundedAgent</strong><small>电商售后客服</small></span>
      </div>
      <h1>登录</h1>
      <p class="helper">顾客账号进入售后会话，操作员账号进入审批台。演示账号见 README。</p>
      <p v-if="notice || error" class="login-error" role="alert">{{ error || notice }}</p>
      <label for="username">用户名</label>
      <input id="username" v-model="username" autocomplete="username" :disabled="busy || checking" required />
      <label for="password">密码</label>
      <input id="password" v-model="password" type="password" autocomplete="current-password" :disabled="busy || checking" required />
      <button class="primary" type="submit" :disabled="busy || checking || !username.trim() || !password">
        {{ checking ? '正在检查登录状态…' : busy ? '正在登录…' : '登录' }}
      </button>
    </form>
  </main>
</template>
