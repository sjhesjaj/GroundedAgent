<script setup lang="ts">
// One login for both roles: a customer account lands in the service center, an
// operator account in the approval desk (router.ts). The server checks the
// role again on every request.
import { Box, DocumentChecked, Lock, Stamp, User } from '@element-plus/icons-vue'
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { homeOf } from '@/router'
import { useAuthStore } from '@/stores/auth'

interface DemoAccount {
  username: string
  password: string
  label: string
  lands: string
}

// The README's demo accounts. Shown in `pnpm dev` and in a build made with
// VITE_DEMO_ACCOUNTS=true. An ordinary production build does not even contain
// them: the condition is a build-time constant there, and the list is dropped.
const showDemoAccounts = import.meta.env.DEV || import.meta.env.VITE_DEMO_ACCOUNTS === 'true'
const DEMO_ACCOUNTS: DemoAccount[] = showDemoAccounts
  ? [
      { username: 'customer-a', password: 'demo-a-2026', label: '演示顾客甲', lands: '售后服务中心' },
      { username: 'customer-b', password: 'demo-b-2026', label: '演示顾客乙', lands: '售后服务中心' },
      { username: 'operator', password: 'operator-2026', label: '售后操作员', lands: '审批台' },
    ]
  : []

const FEATURES = [
  { icon: Box, title: '订单、商品和包裹一屏看清', text: '先找到要处理的那件商品，再发起售后，不用记订单号。' },
  { icon: DocumentChecked, title: '每笔申请先过规则校验', text: '退换货条件由系统按已发布的规则判断，并给出依据。' },
  { icon: Stamp, title: '关键操作由人工审批', text: '需要审批的申请经操作员复核后才执行，全程留有记录。' },
]

const auth = useAuthStore()
const router = useRouter()

const form = reactive({ username: '', password: '' })
const busy = ref(false)
const error = ref('')

const canSubmit = computed(() => !busy.value && form.username.trim() !== '' && form.password !== '')
const alert = computed(() => (error.value
  ? { type: 'error' as const, text: error.value }
  : auth.notice ? { type: 'warning' as const, text: auth.notice } : null))

function fill(account: DemoAccount) {
  form.username = account.username
  form.password = account.password
  error.value = ''
}

async function submit() {
  if (!canSubmit.value) return
  busy.value = true
  error.value = ''
  try {
    const user = await auth.login(form.username.trim(), form.password)
    form.password = ''
    await router.replace(homeOf(user))
  } catch (failure) {
    error.value = (failure as Error).message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="hero" aria-labelledby="hero-title">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">G</span>
        <span class="brand-name"><strong>GroundedAgent</strong><small>售后服务中心</small></span>
      </div>
      <div class="hero-copy">
        <h2 id="hero-title">售后问题，从订单开始</h2>
        <p>选中要处理的商品，就能发起退货、换货或人工协助。</p>
        <ul class="features">
          <li v-for="feature in FEATURES" :key="feature.title">
            <el-icon class="feature-icon" :size="20" aria-hidden="true"><component :is="feature.icon" /></el-icon>
            <span><strong>{{ feature.title }}</strong><small>{{ feature.text }}</small></span>
          </li>
        </ul>
      </div>
      <p class="hero-foot">演示环境 · 订单与商品均为示例数据</p>
    </section>

    <section class="panel">
      <div class="card">
        <h1>登录</h1>
        <p class="subtitle">顾客账号进入售后服务中心，操作员账号进入审批台。</p>

        <el-alert v-if="alert" class="alert" :type="alert.type" :title="alert.text" :closable="false" show-icon
          role="alert" />

        <el-form class="form" label-position="top" size="large" :disabled="busy" @submit.prevent="submit">
          <el-form-item label="账号" for="login-username">
            <el-input id="login-username" v-model="form.username" :prefix-icon="User" placeholder="请输入账号"
              autocomplete="username" />
          </el-form-item>
          <el-form-item label="密码" for="login-password">
            <el-input id="login-password" v-model="form.password" :prefix-icon="Lock" type="password" show-password
              placeholder="请输入密码" autocomplete="current-password" />
          </el-form-item>
          <el-button class="submit" type="primary" native-type="submit" :loading="busy" :disabled="!canSubmit">
            {{ busy ? '正在登录…' : '登录' }}
          </el-button>
        </el-form>

        <div v-if="showDemoAccounts" class="demo">
          <el-divider>演示账号</el-divider>
          <div class="demo-list">
            <button v-for="account in DEMO_ACCOUNTS" :key="account.username" type="button" class="demo-account"
              :disabled="busy" :aria-label="`填入${account.label}的账号和密码`" @click="fill(account)">
              <strong>{{ account.label }}</strong>
              <small>{{ account.username }}</small>
              <span class="lands">进入{{ account.lands }}</span>
            </button>
          </div>
          <p class="demo-note">点击填入账号和密码，再点「登录」。</p>
        </div>
      </div>
    </section>
  </main>
</template>

<style scoped>
.login-page {
  display: grid;
  grid-template-columns: minmax(360px, 5fr) 7fr;
  min-height: 100dvh;
}

/* -- the brand side ------------------------------------------------------ */
.hero {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 48px;
  padding: 48px 56px;
  color: #fff;
  background:
    radial-gradient(120% 80% at 100% 0%, rgb(255 255 255 / 12%), transparent 60%),
    linear-gradient(160deg, #8f3412 0%, #b4481b 55%, #c8501e 100%);
}
.brand { display: flex; align-items: center; gap: 12px; }
.brand-mark {
  display: grid; place-items: center; width: 40px; height: 40px;
  border-radius: 12px; background: #fff; color: var(--el-color-primary);
  font-weight: 700; font-size: 22px;
}
.brand-name strong { display: block; font-size: 18px; letter-spacing: -0.2px; }
.brand-name small { display: block; margin-top: 2px; font-size: 12px; opacity: 0.85; letter-spacing: 2px; }
.hero-copy h2 { margin: 0; font-size: 34px; line-height: 1.25; font-weight: 700; letter-spacing: 1px; }
.hero-copy > p { margin: 14px 0 0; font-size: 15px; line-height: 1.7; opacity: 0.92; }
.features { display: grid; gap: 22px; margin: 40px 0 0; padding: 0; list-style: none; }
.features li { display: flex; gap: 14px; align-items: flex-start; }
.feature-icon {
  flex: none; width: 40px; height: 40px; border-radius: 10px;
  background: rgb(255 255 255 / 14%);
}
.features strong { display: block; font-size: 15px; }
.features small { display: block; margin-top: 4px; font-size: 13px; line-height: 1.6; opacity: 0.88; }
.hero-foot { margin: 0; font-size: 12px; opacity: 0.75; }

/* -- the form side ------------------------------------------------------- */
/* minmax(0, 1fr): the track follows the screen, never the card's 460px. */
.panel { display: grid; grid-template-columns: minmax(0, 1fr); place-items: center; padding: 48px 24px; }
.card {
  width: min(460px, 100%);
  padding: 40px 36px 32px;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 16px;
  box-shadow: var(--app-shadow);
}
.card h1 { margin: 0; font-size: 26px; font-weight: 700; }
.subtitle { margin: 8px 0 24px; color: var(--app-text-muted); font-size: 14px; line-height: 1.6; }
.alert { margin-bottom: 20px; }
.form :deep(.el-form-item__label) { font-weight: 600; color: var(--app-text); }
.submit { width: 100%; margin-top: 8px; height: 44px; font-size: 16px; letter-spacing: 4px; }

.demo { margin-top: 28px; }
.demo :deep(.el-divider__text) { color: var(--app-text-muted); font-size: 13px; background: var(--app-surface); }
.demo-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.demo-account {
  display: flex; flex-direction: column; align-items: flex-start; gap: 2px;
  padding: 12px; text-align: left; cursor: pointer;
  font: inherit; color: var(--app-text);
  background: var(--app-surface-muted);
  border: 1px solid var(--app-border); border-radius: 10px;
  transition: border-color 0.15s, background-color 0.15s;
}
.demo-account:hover:not(:disabled) { border-color: var(--el-color-primary-light-5); background: var(--el-color-primary-light-9); }
.demo-account:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.demo-account:disabled { cursor: not-allowed; opacity: 0.6; }
.demo-account strong { font-size: 14px; }
.demo-account small { color: var(--app-text-muted); font-size: 12px; font-family: "Cascadia Code", Consolas, monospace; }
.demo-account .lands { margin-top: 6px; color: var(--el-color-primary-dark-2); font-size: 12px; }
.demo-note { margin: 12px 0 0; color: var(--app-text-muted); font-size: 12px; text-align: center; }

/* -- narrow screens: the brand side becomes a header --------------------- */
@media (max-width: 900px) {
  .login-page { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto 1fr; }
  .hero { gap: 0; padding: 20px 16px; }
  .hero-copy, .hero-foot { display: none; }
  .panel { align-items: start; padding: 24px 16px 40px; }
  .card { padding: 28px 20px 24px; }
}
@media (max-width: 420px) {
  .demo-list { grid-template-columns: 1fr; }
  .demo-account { flex-direction: row; flex-wrap: wrap; align-items: baseline; gap: 8px; }
  .demo-account .lands { margin: 0 0 0 auto; }
}
</style>
