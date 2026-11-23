<script setup lang="ts">
// The operator's approval desk: the session queue (待审批 / 进行中 / 全部), the
// selected session's transcript, and 审批详情 for one of its actions with the
// order line it names. A decision is only ever POST …/decision, asked for after
// a confirmation, never retried; the page then re-reads what the server kept.
// The approver recorded with a decision is the logged-in operator's (from the
// token, server-side); nothing here names it.
//
// The order lookup (GET …/order-summary) is display only: when it answers null
// or 409 recovery_pending, the transcript and the action stay, the panel says
// so and offers to read again, and the decision buttons stay as they are.
import { ArrowDown, Refresh } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { aftersalesApi, type AftersalesApiError } from '@/api'
import AppTopBar from '@/components/AppTopBar.vue'
import ConfirmDialog from '@/components/operator/ConfirmDialog.vue'
import ReviewPanel from '@/components/operator/ReviewPanel.vue'
import SessionQueue from '@/components/operator/SessionQueue.vue'
import Transcript from '@/components/operator/Transcript.vue'
import { useAuthStore } from '@/stores/auth'
import type { ActionView, Decision, OperatorSessionSummary, SessionView } from '@/types'
import {
  confirmCopy, decisionToast, focusAction,
  type Confirming, type QueueTab, type SummaryState, type ToastKind,
} from '@/utils/desk'
import { dateTime, money } from '@/utils/format'

const auth = useAuthStore()
// The account this page was opened for; logging out leaves the page.
const user = auth.user!

const sessions = ref<OperatorSessionSummary[]>([])
const sessionsLoading = ref(true)
const tab = ref<QueueTab>('waiting')
const selectedId = ref('')
const view = ref<SessionView | null>(null)
const focusId = ref<string | null>(null)
const summary = ref<SummaryState>({ state: 'idle' })
const busy = ref<'' | 'sync' | 'decision' | 'reset'>('')
const notice = ref('')
const confirming = ref<{ kind: Confirming, action: ActionView | null } | null>(null)
const dialogOpen = ref(false)
const toast = ref<{ kind: ToastKind, text: string } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | undefined

const actions = computed(() => view.value?.pending_actions ?? [])
const action = computed(() => actions.value.find((entry) => entry.pending_action_id && entry.pending_action_id === focusId.value)
  ?? focusAction(actions.value))
const waitingCount = computed(() => sessions.value.reduce((total, item) => total + (item.waiting_approval?.length ?? 0), 0))
const businessTime = computed(() => dateTime(view.value?.business_time))

const copy = computed(() => confirming.value ? confirmCopy(confirming.value.kind, confirming.value.action) : null)
const subject = computed(() => {
  const target = confirming.value?.action
  if (!target) return null
  const who = view.value?.persona.display_name ?? ''
  const data = 'data' in summary.value && summary.value.data.pending_action_id === target.pending_action_id ? summary.value.data : null
  if (data?.item) {
    return {
      title: `${data.item.product_name} × ${data.item.quantity}`,
      detail: `${who} · 订单 ${data.order?.order_id ?? target.arguments.order_id} · 商品小计 ${money(data.item.subtotal)}`,
      category: data.item.category,
    }
  }
  const line = target.arguments.order_item_id ? ` · 商品行 ${target.arguments.order_item_id}` : ''
  return { title: `订单 ${target.arguments.order_id ?? '—'}${line}`, detail: `${who} · 订单信息暂时读不到，以申请参数为准` }
})

function show(next: { kind: ToastKind, text: string }) {
  toast.value = next
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = null }, 3200)
}

const message = (failure: unknown) => (failure as AftersalesApiError).message

async function loadSessions() {
  try {
    sessions.value = (await aftersalesApi.operatorSessions()).items
  } finally {
    sessionsLoading.value = false
  }
}

async function loadSelected() {
  if (!selectedId.value) {
    view.value = null
    return
  }
  // A failed read keeps what is on screen: the transcript and the action stay.
  view.value = await aftersalesApi.operatorSession(selectedId.value)
  if (!actions.value.some((entry) => entry.pending_action_id && entry.pending_action_id === focusId.value)) {
    focusId.value = focusAction(actions.value)?.pending_action_id ?? null
  }
}

let summaryTicket = 0
async function loadSummary() {
  const ticket = ++summaryTicket
  const sessionId = view.value?.session_id
  const pendingId = action.value?.pending_action_id
  if (!sessionId || !pendingId) {
    summary.value = { state: 'idle' }
    return
  }
  summary.value = { state: 'loading' }
  try {
    const data = await aftersalesApi.orderSummary(sessionId, pendingId)
    if (ticket === summaryTicket) summary.value = data.order && data.item ? { state: 'ready', data } : { state: 'missing', data }
  } catch (failure) {
    if (ticket !== summaryTicket) return
    const error = failure as AftersalesApiError
    summary.value = error.code === 'recovery_pending' ? { state: 'recovering' } : { state: 'error', message: error.message }
  }
}

async function refresh() {
  if (busy.value) return
  busy.value = 'sync'
  notice.value = ''
  try {
    await loadSessions()
    if (selectedId.value && !sessions.value.some((item) => item.session_id === selectedId.value)) {
      selectedId.value = ''
      focusId.value = null
    }
    await loadSelected()
  } catch (failure) {
    notice.value = message(failure)
  } finally {
    busy.value = ''
  }
  await loadSummary()
}

async function select(sessionId: string) {
  if (busy.value || sessionId === selectedId.value) return
  selectedId.value = sessionId
  view.value = null
  focusId.value = null
  summary.value = { state: 'idle' }
  busy.value = 'sync'
  notice.value = ''
  try {
    await loadSelected()
  } catch (failure) {
    notice.value = message(failure)
  } finally {
    busy.value = ''
  }
  await loadSummary()
}

function focus(pendingActionId: string) {
  if (pendingActionId === action.value?.pending_action_id) return
  focusId.value = pendingActionId
  loadSummary()
}

function ask(kind: Confirming) {
  if (busy.value) return
  confirming.value = { kind, action: kind === 'RESET' ? null : action.value }
  dialogOpen.value = true
}

async function confirm() {
  const pending = confirming.value
  dialogOpen.value = false
  if (!pending) return
  if (pending.kind === 'RESET') await resetDemo()
  else if (pending.action) await decide(pending.action, pending.kind)
}

async function decide(target: ActionView, decision: Decision) {
  if (busy.value || !view.value || target.status !== 'WAITING_APPROVAL' || !target.pending_action_id) return
  busy.value = 'decision'
  notice.value = ''
  try {
    show(decisionToast(await aftersalesApi.decide(view.value.session_id, target.pending_action_id, decision)))
  } catch (failure) {
    notice.value = message(failure)
  } finally {
    // Read the persisted outcome either way; never retry the decision automatically.
    try {
      await loadSessions()
      await loadSelected()
    } catch (failure) {
      notice.value = message(failure)
    }
    busy.value = ''
  }
  await loadSummary()
}

async function resetDemo() {
  if (busy.value) return
  busy.value = 'reset'
  notice.value = ''
  try {
    await aftersalesApi.resetDemo()
    selectedId.value = ''
    view.value = null
    focusId.value = null
    summary.value = { state: 'idle' }
    await loadSessions()
    show({ kind: 'success', text: '演示数据已重置。' })
  } catch (failure) {
    notice.value = message(failure)
  } finally {
    busy.value = ''
  }
}

function onMore(command: string) {
  if (command === 'reset') ask('RESET')
}

onMounted(refresh)
onBeforeUnmount(() => clearTimeout(toastTimer))
</script>

<template>
  <div class="desk-page">
    <AppTopBar page="售后审批台" :user="user" :business-time="businessTime" initial="审" @logout="auth.logout()">
      <span class="stat warn">待审批 <b>{{ waitingCount }}</b></span>
      <span class="stat">会话 <b>{{ sessions.length }}</b></span>
      <span class="tools">
        <el-button class="refresh" :icon="Refresh" :loading="busy === 'sync'" :disabled="Boolean(busy)" @click="refresh">刷新</el-button>
        <el-dropdown trigger="click" placement="bottom-end" :disabled="Boolean(busy)" @command="onMore">
          <el-button class="more">更多<el-icon class="el-icon--right"><ArrowDown /></el-icon></el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="reset" class="reset-item">
                <span class="reset-text"><strong>重置演示数据</strong><small>清空所有会话和模拟售后记录</small></span>
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </span>
    </AppTopBar>

    <div v-if="notice" class="notice" role="alert">
      <span>{{ notice }}</span>
      <button type="button" class="dismiss" aria-label="关闭提示" @click="notice = ''">×</button>
    </div>

    <main class="desk" :aria-busy="Boolean(busy)">
      <SessionQueue v-model:tab="tab" class="queue" :sessions="sessions" :selected-id="selectedId" :loading="sessionsLoading"
        :disabled="Boolean(busy)" @select="select" />
      <Transcript class="transcript" :view="view" :focus-id="action?.pending_action_id ?? null" :loading="busy === 'sync' && !view"
        @focus="focus" />
      <ReviewPanel v-if="view" class="review" :actions="actions" :action="action" :audit="view.audit" :summary="summary"
        :busy="Boolean(busy)" :deciding="busy === 'decision'" @focus="focus" @decide="ask" @reload="loadSummary" />
      <aside v-else class="review review-empty" aria-label="审批详情"><p>选择会话后，这里显示待审批的申请和订单核对信息。</p></aside>
    </main>

    <ConfirmDialog v-model="dialogOpen" :copy="copy" :subject="subject" @confirm="confirm" />

    <Transition name="toast">
      <div v-if="toast" class="toast" :class="toast.kind" role="status">{{ toast.text }}</div>
    </Transition>
  </div>
</template>

<style scoped>
.desk-page { display: flex; flex-direction: column; height: 100vh; background: var(--app-bg); color: var(--app-text); }
.stat {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px;
  border-radius: 15px;
  background: var(--app-bg);
  color: var(--app-text-body);
  font-size: 13px;
  white-space: nowrap;
}
.stat b { font-variant-numeric: tabular-nums; }
.stat.warn { background: var(--tone-amber-bg); color: var(--tone-amber); }
.tools { display: inline-flex; gap: 8px; margin-left: auto; }
.tools :deep(.el-button + .el-button) { margin-left: 0; }
.reset-text { display: flex; flex-direction: column; gap: 2px; padding: 4px 0; line-height: 1.4; }
.reset-text strong { color: var(--tone-red); font-size: 14px; }
.reset-text small { color: var(--app-text-muted); font-size: 12px; }
.notice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 24px;
  background: var(--tone-red-bg);
  color: var(--tone-red);
  font-size: 13px;
  flex: none;
}
.dismiss { border: 0; background: none; color: inherit; font-size: 18px; line-height: 1; cursor: pointer; }
.desk { display: grid; flex: 1; grid-template-columns: 300px minmax(0, 1fr) 420px; min-height: 0; }
.review-empty { display: grid; place-items: center; padding: 40px; background: var(--app-surface); border-left: 1px solid var(--app-border); color: var(--app-text-muted); font-size: 14px; line-height: 1.8; text-align: center; }
.toast {
  position: fixed;
  top: 80px;
  left: 50%;
  z-index: 3000;
  padding: 10px 16px;
  border-radius: 10px;
  background: #1f1a17;
  color: #fff;
  font-size: 14px;
  box-shadow: 0 12px 32px rgb(31 26 23 / 24%);
  transform: translateX(-50%);
}
.toast.warning { background: #5c3d06; }
.toast.error { background: #7a1a12; }
.toast-enter-active, .toast-leave-active { transition: opacity var(--motion-base), transform var(--motion-base) var(--motion-ease); }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translate(-50%, -8px); }

/* Narrower screens: the queue stays on the left, 审批详情 goes under the transcript. */
@media (max-width: 1180px) {
  /* The top bar keeps what a decision needs: 待审批, 刷新, 更多 and the account. */
  .desk-page :deep(.top-bar .time), .stat:not(.warn) { display: none; }
  .desk-page { height: auto; min-height: 100vh; }
  .desk { grid-template-columns: 260px minmax(0, 1fr); grid-template-areas: "queue transcript" "queue review"; }
  .queue { grid-area: queue; }
  .transcript { grid-area: transcript; height: 60vh; }
  .review { grid-area: review; border-top: 1px solid var(--app-border); border-left: 0; }
}
@media (max-width: 760px) {
  .desk { grid-template-columns: minmax(0, 1fr); grid-template-areas: "queue" "transcript" "review"; }
  .queue { max-height: 45vh; border-right: 0; border-bottom: 1px solid var(--app-border); }
}
</style>
