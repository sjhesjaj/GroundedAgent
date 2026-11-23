<script setup lang="ts">
// The customer's 售后服务中心. Who the customer is comes from the login token
// (the server binds every new session to it); this page never names a customer
// and has no approval controls: operators decide.
//
// Left: 我的订单, the entry point. A product's 申请售后 / 咨询 only prepare a
// draft; the customer sends it. Right: the 智能售后助手 conversation with the
// request's progress card. Once a conversation has started it becomes the main
// column. On a phone the orders are the page and the conversation opens full
// screen.
//
// The session logic (start-up, reconnect, send, read back after a failure) is
// the pre-redesign page's, unchanged.
import { ArrowLeft, ChatDotRound, Document, MoreFilled, Refresh } from '@element-plus/icons-vue'
import { computed, nextTick, onMounted, ref } from 'vue'

import { aftersalesApi, type AftersalesApiError } from '@/api'
import AppTopBar from '@/components/AppTopBar.vue'
import StatusTag from '@/components/StatusTag.vue'
import ApplicationCard from '@/components/customer/ApplicationCard.vue'
import ApplySheet from '@/components/customer/ApplySheet.vue'
import ChatComposer from '@/components/customer/ChatComposer.vue'
import EvidenceDrawer from '@/components/customer/EvidenceDrawer.vue'
import OrderList from '@/components/customer/OrderList.vue'
import ProductThumb from '@/components/customer/ProductThumb.vue'
import { connectPage, recoveryAction, rememberSession } from '@/sessionMemory'
import { useAuthStore } from '@/stores/auth'
import type {
  ActionView, AuditEvent, Citation, DemoInfo, Message, Order, OrderItem, SessionView, Trace, TurnResponse,
} from '@/types'
import { askDraft, composeDraft, type Attachment } from '@/utils/draft'
import { dateTime, money } from '@/utils/format'
import { describeAction, lineBadges, type NextKind, type Tone } from '@/utils/outcome'

const auth = useAuthStore()
// The account this page was opened for; logging out leaves the page.
const user = auth.user!

type Header = Pick<SessionView, 'session_id' | 'persona' | 'business_time' | 'status' | 'pending_action_id'>

interface Entry extends Message {
  id: number
  actionKey?: string | null
  citations?: Citation[]
  trace?: Trace | null
  traceAction?: ActionView | null
  attachment?: Attachment | null
}

type Busy = '' | 'startup' | 'session' | 'sync' | 'message'

const demo = ref<DemoInfo | null>(null)
const session = ref<Header | null>(null)
const messages = ref<Entry[]>([])
const actions = ref<Record<string, ActionView>>({})
const audit = ref<AuditEvent[]>([])
const question = ref('')
const busy = ref<Busy>('startup')
const notice = ref('')
const invalidSession = ref(false)
const policyVersionMismatch = ref(false)
const syncRequired = ref(false)
const messageList = ref<HTMLElement | null>(null)
const composer = ref<InstanceType<typeof ChatComposer> | null>(null)
let messageSequence = 0

// -- the order list and the drafts it prepares -----------------------------------

const orders = ref<Order[]>([])
const ordersLoading = ref(true)
const ordersError = ref('')
const attachment = ref<Attachment | null>(null)
const sending = ref<{ text: string, attachment: Attachment | null } | null>(null)
const panelOpen = ref(false)
const sheet = ref<{ open: boolean, order: Order | null, item: OrderItem | null }>({ open: false, order: null, item: null })
const evidence = ref<{ open: boolean, action: ActionView | null, citations: Citation[], trace: Trace | null, subject: string }>(
  { open: false, action: null, citations: [], trace: null, subject: '' })

const QUESTIONS = ['我的包裹到哪了？', '退货需要满足什么条件？', '贴身衣物可以退吗？', '怎么联系人工客服？']
const statusLabels: Record<string, string> = { OPEN: '会话进行中', NEEDS_CLARIFICATION: '等待补充信息', WAITING_APPROVAL: '等待人工审核' }
const statusTones: Record<string, Tone> = { OPEN: 'green', NEEDS_CLARIFICATION: 'peach', WAITING_APPROVAL: 'amber' }

const locked = computed(() => Boolean(busy.value))
const canInteract = computed(() => Boolean(session.value) && !invalidSession.value && !policyVersionMismatch.value && !syncRequired.value && !locked.value)
const canSend = computed(() => canInteract.value && question.value.trim().length > 0 && question.value.trim().length <= 2000)
const unusable = computed(() => invalidSession.value || policyVersionMismatch.value || syncRequired.value)
const sessionStatus = computed(() => policyVersionMismatch.value ? '请新建会话' : (invalidSession.value ? '会话已失效' : (syncRequired.value ? '等待同步状态' : (session.value ? statusLabels[session.value.status] || session.value.status : '尚未连接'))))
const sessionTone = computed<Tone>(() => unusable.value ? 'red' : (session.value ? statusTones[session.value.status] ?? 'gray' : 'gray'))
const businessTime = computed(() => dateTime(session.value?.business_time || demo.value?.business_time || ''))
const unplacedActions = computed(() => Object.entries(actions.value).filter(([key]) => !messages.value.some((message) => message.actionKey === key)))
const recovery = computed(() => policyVersionMismatch.value ? 'new_session' : recoveryAction({
  hasSession: Boolean(session.value), invalidSession: invalidSession.value, syncRequired: syncRequired.value,
}))
const conversing = computed(() => messages.value.length > 0 || Boolean(sending.value))

function findLine(orderId: string | undefined, itemId: string | undefined) {
  const order = orders.value.find((entry) => entry.order_id === orderId)
  const item = order?.items.find((entry) => entry.order_item_id === itemId)
  return order && item ? { order, item } : null
}

/** Each product line with a request in this session carries one badge (utils/outcome.ts lineBadges). */
const badges = computed(() => lineBadges(Object.values(actions.value)))
const activeRequests = computed(() => Object.values(actions.value).filter((action) => action.status === 'WAITING_APPROVAL').length)

async function scrollToEnd() {
  await nextTick()
  const list = messageList.value
  if (list) list.scrollTop = list.scrollHeight
}

function updateHeader(data: Header) {
  session.value = {
    session_id: data.session_id, persona: data.persona, business_time: data.business_time,
    status: data.status, pending_action_id: data.pending_action_id,
  }
}

// GET /sessions owns the transcript; rich metadata survives only when the
// corresponding entry still matches, since the server does not persist trace.
function reconcile(data: SessionView) {
  const previous = messages.value
  updateHeader(data)
  for (const action of data.pending_actions || []) {
    if (action.pending_action_id) actions.value[action.pending_action_id] = action
  }
  const placed = new Set<string>()
  messages.value = data.messages.map((entry, index) => {
    const old = previous[index]
    const same = old?.role === entry.role && old?.text === entry.text
    const item: Entry = { ...(same ? old : {}), ...entry, id: same ? old.id : ++messageSequence }
    const key = entry.pending_action_id || (same ? old.actionKey : null)
    item.actionKey = entry.role === 'assistant' && key && !placed.has(key) ? key : null
    if (item.actionKey) placed.add(item.actionKey)
    return item
  })
  audit.value = data.audit || []
  invalidSession.value = false
  policyVersionMismatch.value = false
  syncRequired.value = false
}

function activate(data: SessionView) {
  messages.value = []
  actions.value = {}
  audit.value = []
  question.value = ''
  attachment.value = null
  reconcile(data)
  // Survives a page reload; the server keeps the session across restarts.
  rememberSession(data.session_id)
}

function applyResponse(data: TurnResponse, customerText?: string, attached: Attachment | null = null) {
  updateHeader(data)
  if (customerText !== undefined) messages.value.push({ id: ++messageSequence, role: 'customer', text: customerText, attachment: attached })
  const actionKey = data.action ? (data.action.pending_action_id || `turn-${++messageSequence}`) : null
  if (actionKey && data.action) actions.value[actionKey] = data.action
  // Replays/conflicts return a reply but do not append to the server transcript.
  const replayedDecision = 'operator_decision' in data && (data.action?.idempotent_replay || data.action?.decision_conflict)
  if (replayedDecision) notice.value = data.reply.text
  else messages.value.push({
    id: ++messageSequence, role: 'assistant', text: data.reply.text, kind: data.reply.kind,
    citations: data.citations, trace: data.trace, traceAction: data.action,
    pending_action_id: data.action?.pending_action_id,
    actionKey: actionKey && !messages.value.some((message) => message.actionKey === actionKey) ? actionKey : null,
  })
  audit.value = data.audit || []
}

function showError(error: AftersalesApiError) {
  notice.value = error.message
  if (error.code === 'session_not_found') invalidSession.value = true
  if (error.code === 'policy_version_mismatch') {
    policyVersionMismatch.value = true
    syncRequired.value = false
  }
}

// Page start-up and "重新连接": back to the remembered session while the
// server has it; a new session only when it is gone (404).
async function connect() {
  busy.value = 'startup'
  notice.value = ''
  try {
    const { demo: loaded, data } = await connectPage(aftersalesApi)
    demo.value = loaded
    activate(data)
    // A resumed conversation opens on its latest message.
    await scrollToEnd()
  } catch (error) {
    showError(error as AftersalesApiError)
  } finally {
    busy.value = ''
  }
}

async function newSession() {
  if (locked.value) return
  busy.value = 'session'
  notice.value = ''
  try {
    activate(await aftersalesApi.createSession())
  } catch (error) {
    showError(error as AftersalesApiError)
  } finally {
    busy.value = ''
  }
}

async function recoverSession() {
  syncRequired.value = true
  try {
    reconcile(await aftersalesApi.session(session.value!.session_id))
  } catch (failure) {
    const error = failure as AftersalesApiError
    if (error.code === 'session_not_found' || error.code === 'policy_version_mismatch') showError(error)
    else notice.value += ' 无法同步会话，请先刷新状态再继续。'
  }
}

async function refreshSession() {
  if (locked.value || !session.value) return
  busy.value = 'sync'
  notice.value = ''
  const before = messages.value.length
  await recoverSession()
  busy.value = ''
  // A decision arrives as a new message at the end: bring it into view.
  if (messages.value.length > before) await scrollToEnd()
}

async function send() {
  if (!canSend.value) return
  const text = question.value.trim()
  const attached = attachment.value
  // Once the message is in, clear the composer only if it still holds what
  // was sent: a draft prepared while waiting (咨询, 申请售后) is the next message.
  const clearSent = () => {
    if (question.value.trim() === text) question.value = ''
    if (attachment.value === attached) attachment.value = null
  }
  busy.value = 'message'
  notice.value = ''
  sending.value = { text, attachment: attached }
  await scrollToEnd()
  try {
    applyResponse(await aftersalesApi.sendMessage(session.value!.session_id, text), text, attached)
    clearSent()
  } catch (failure) {
    const error = failure as AftersalesApiError
    showError(error)
    // An interrupted response may already have committed an action. Read its
    // persisted outcome instead of automatically retrying the mutation.
    if (!error.status || error.status >= 500 || error.code === 'invalid_response') {
      const oldCount = messages.value.length
      await recoverSession()
      if (messages.value.slice(oldCount).some((entry) => entry.role === 'customer' && entry.text === text)) clearSent()
    }
  } finally {
    sending.value = null
    busy.value = ''
    await scrollToEnd()
    composer.value?.focus()
  }
}

async function draft(text: string, attached: Attachment | null) {
  question.value = text
  attachment.value = attached
  panelOpen.value = true
  await nextTick()
  composer.value?.focus()
}

async function chooseSuggestion(text: string) {
  if (!canInteract.value) return
  await draft(text, null)
}

function onCommand(command: string) {
  if (command === 'new') newSession()
  else if (command === 'refresh') refreshSession()
}

// -- orders, drafts, progress and evidence ---------------------------------------

async function loadOrders() {
  ordersLoading.value = true
  ordersError.value = ''
  try {
    orders.value = (await aftersalesApi.orders()).orders
  } catch (error) {
    ordersError.value = (error as AftersalesApiError).message
  } finally {
    ordersLoading.value = false
  }
}

function openSheet(order: Order, item: OrderItem) {
  sheet.value = { open: true, order, item }
}

function useSheetDraft(text: string) {
  const { order, item } = sheet.value
  draft(text, order && item ? { order_id: order.order_id, item } : null)
}

function ask(order: Order, item: OrderItem) {
  draft(askDraft({ order_id: order.order_id, product_name: item.product_name }), { order_id: order.order_id, item })
}

function attach(choice: Attachment) {
  attachment.value = choice
  if (!question.value.trim()) question.value = askDraft({ order_id: choice.order_id, product_name: choice.item.product_name })
  composer.value?.focus()
}

function lineOf(action: ActionView) {
  return findLine(action.arguments?.order_id, action.arguments?.order_item_id)
}

function nextStep(action: ActionView, kind: NextKind) {
  const line = lineOf(action)
  if (kind === 'refresh') refreshSession()
  else if (kind === 'reapply' && line) openSheet(line.order, line.item)
  else if (kind === 'human') {
    draft(line ? composeDraft('human', { order_id: line.order.order_id, product_name: line.item.product_name }, null) : '我需要人工协助。',
      line ? { order_id: line.order.order_id, item: line.item } : null)
  } else draft('', null)
}

function showEvidence(entry: Entry | null, action: ActionView | null) {
  const owner = entry ?? messages.value.find((message) => action && message.actionKey === action.pending_action_id) ?? null
  const shown = action ?? owner?.traceAction ?? null
  const line = shown ? lineOf(shown) : null
  const subject = shown
    ? [describeAction(shown).title, line?.item.product_name, shown.arguments?.order_id && `订单 ${shown.arguments.order_id}`].filter(Boolean).join(' · ')
    : '这条回复'
  evidence.value = { open: true, action: shown, citations: owner?.citations ?? [], trace: owner?.trace ?? null, subject }
}

// A reply with a progress card links its evidence from the card instead.
const hasEvidence = (entry: Entry) => entry.role === 'assistant' && !(entry.actionKey && actions.value[entry.actionKey])
  && Boolean(entry.citations?.length || entry.trace || entry.traceAction)

onMounted(connect)
onMounted(loadOrders)
</script>

<template>
  <div class="service-center" :class="{ conversing }">
    <AppTopBar page="售后服务中心" :user="user" :business-time="businessTime" @logout="auth.logout()" />

    <main class="layout">
      <OrderList class="orders" :orders="orders" :loading="ordersLoading" :error="ordersError" :badges="badges"
        :subtitle="conversing ? '正在办理的售后会标在对应的商品上。' : ''" @apply="openSheet" @ask="ask" @retry="loadOrders" />

      <section class="chat-panel" :class="{ open: panelOpen }" aria-label="智能售后助手">
        <header class="assistant-head">
          <el-button class="back" text circle :icon="ArrowLeft" aria-label="返回我的订单" @click="panelOpen = false" />
          <span class="mark" aria-hidden="true">G<span class="online"></span></span>
          <div class="assistant-title">
            <strong>智能售后助手</strong>
            <StatusTag class="state" :tone="sessionTone" :pulse="session?.status === 'WAITING_APPROVAL'">{{ sessionStatus }}</StatusTag>
          </div>
          <el-tooltip content="刷新状态" placement="bottom">
            <el-button text circle :icon="Refresh" aria-label="刷新状态" :disabled="locked || !session" @click="refreshSession" />
          </el-tooltip>
          <el-dropdown trigger="click" placement="bottom-end" @command="onCommand">
            <el-button text circle :icon="MoreFilled" aria-label="更多操作" />
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="new" :disabled="locked">新建会话</el-dropdown-item>
                <el-dropdown-item command="refresh" :disabled="locked || !session">刷新状态</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </header>

        <div v-if="notice" class="notice" role="alert">
          <span>{{ notice }}</span>
          <button v-if="recovery === 'refresh'" type="button" class="notice-action" :disabled="locked" @click="refreshSession">刷新状态</button>
          <button v-else-if="recovery === 'new_session'" type="button" class="notice-action" :disabled="locked" @click="newSession()">新建会话</button>
          <button v-else-if="recovery === 'reconnect'" type="button" class="notice-action" :disabled="locked" @click="connect">重新连接</button>
          <button v-else type="button" class="notice-dismiss" aria-label="关闭提示" @click="notice = ''">×</button>
        </div>

        <div ref="messageList" class="thread" :aria-busy="locked" aria-live="polite">
          <section v-if="!conversing" class="welcome">
            <h2>你好，{{ user.display_name }}</h2>
            <p>在订单里选中商品点「申请售后」，或者直接问我。退货、换货会先核对订单和规则，需要时转给售后专员审核。</p>
            <span class="label">常见问题</span>
            <div class="questions">
              <button v-for="text in QUESTIONS" :key="text" type="button" class="question" :disabled="!canInteract"
                @click="chooseSuggestion(text)">{{ text }}</button>
            </div>
            <div class="how">
              <span class="label">售后怎么处理</span>
              <ol>
                <li>智能客服读取订单、物流和售后规则</li>
                <li>符合条件的申请按规则直接办理，或转售后专员审核</li>
                <li>处理结果显示在这里的申请进度卡上</li>
              </ol>
            </div>
          </section>

          <TransitionGroup name="msg" tag="div" class="messages">
            <article v-for="message in messages" :key="message.id" class="message" :class="message.role">
              <span v-if="message.role === 'assistant'" class="avatar" aria-hidden="true">G</span>
              <div class="body">
                <span v-if="message.kind === 'operator_decision'" class="who">审核结果</span>
                <div v-if="message.attachment" class="card">
                  <ProductThumb :category="message.attachment.item.category" :size="40" />
                  <span>{{ message.attachment.item.product_name }}<small>订单 {{ message.attachment.order_id }} · {{ money(message.attachment.item.unit_price) }} × {{ message.attachment.item.quantity }}</small></span>
                </div>
                <p class="bubble">{{ message.text }}</p>
                <el-button v-if="hasEvidence(message)" class="evidence-link" text type="primary" size="small" :icon="Document"
                  @click="showEvidence(message, message.traceAction ?? null)">处理依据</el-button>
                <ApplicationCard v-if="message.actionKey && actions[message.actionKey]" :action="actions[message.actionKey]"
                  :item="lineOf(actions[message.actionKey])?.item" :disabled="locked"
                  @next="(kind) => nextStep(actions[message.actionKey!], kind)" @evidence="showEvidence(message, actions[message.actionKey!])" />
              </div>
            </article>
          </TransitionGroup>

          <ApplicationCard v-for="[key, action] in unplacedActions" :key="key" class="unplaced" :action="action"
            :item="lineOf(action)?.item" :disabled="locked" @next="(kind) => nextStep(action, kind)" @evidence="showEvidence(null, action)" />

          <template v-if="sending">
            <article class="message customer pending">
              <div class="body">
                <div v-if="sending.attachment" class="card">
                  <ProductThumb :category="sending.attachment.item.category" :size="40" />
                  <span>{{ sending.attachment.item.product_name }}<small>订单 {{ sending.attachment.order_id }}</small></span>
                </div>
                <p class="bubble">{{ sending.text }}</p>
              </div>
            </article>
            <div class="message assistant" role="status">
              <span class="avatar" aria-hidden="true">G</span>
              <span class="typing"><i></i><i></i><i></i><span class="sr-only">正在处理…</span></span>
            </div>
          </template>
          <p v-else-if="busy" class="busy" role="status">{{ busy === 'sync' ? '正在同步会话…' : '正在准备会话…' }}</p>
        </div>

        <ChatComposer ref="composer" v-model="question" :attachment="attachment" :orders="orders" :disabled="!canInteract"
          :sending="busy === 'message'" :can-send="canSend" @send="send" @detach="attachment = null" @attach="attach" />
      </section>
    </main>

    <button type="button" class="fab" :class="{ hidden: panelOpen }" @click="panelOpen = true">
      <el-icon aria-hidden="true"><ChatDotRound /></el-icon>智能客服
      <span v-if="activeRequests" class="fab-badge">{{ activeRequests }} 项审核中</span>
    </button>

    <ApplySheet v-model="sheet.open" :order="sheet.order" :item="sheet.item" @use="useSheetDraft" />
    <EvidenceDrawer v-model="evidence.open" :action="evidence.action" :citations="evidence.citations"
      :trace="evidence.trace" :audit="audit" :session-id="session?.session_id" :subject="evidence.subject" />
  </div>
</template>

<style scoped>
.service-center { min-height: 100dvh; background: var(--app-bg); color: var(--app-text); }
.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 440px;
  align-items: start;
  gap: 24px;
  max-width: 1440px;
  margin: 0 auto;
  padding: 24px 32px 48px;
  transition: grid-template-columns var(--motion-slow) var(--motion-ease);
}
/* Once a conversation has started, it and its progress are the main column. */
.conversing .layout { grid-template-columns: minmax(340px, 400px) minmax(0, 1fr); }

.chat-panel {
  position: sticky;
  top: 88px;
  display: flex;
  flex-direction: column;
  height: calc(100dvh - 112px);
  min-height: 560px;
  overflow: hidden;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 14px;
  box-shadow: 0 8px 24px rgb(31 26 23 / 6%);
}
.assistant-head { display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-bottom: 1px solid #efe9e3; }
.assistant-head :deep(.el-button + .el-button) { margin-left: 0; }
.back { display: none; }
.mark {
  position: relative;
  display: grid;
  place-items: center;
  flex: none;
  width: 40px;
  height: 40px;
  border-radius: 12px;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 18px;
  font-weight: 700;
}
.online { position: absolute; right: -3px; bottom: -3px; width: 12px; height: 12px; border: 2px solid #fff; border-radius: 6px; background: var(--tone-green-dot); }
.assistant-title { display: flex; flex: 1; flex-direction: column; align-items: flex-start; gap: 4px; min-width: 0; }
.assistant-title strong { font-size: 15px; }
.state { height: 20px; padding: 0 8px; font-size: 11px; }

.notice {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid #f3dfb8;
  background: #fff8ec;
  color: var(--tone-amber);
  font-size: 13px;
  line-height: 1.6;
  animation: app-rise var(--motion-base) ease-out both;
}
.notice span { flex: 1; }
.notice-action {
  flex: none;
  height: 30px;
  padding: 0 12px;
  border: 1px solid var(--tone-amber-dot);
  border-radius: 8px;
  background: #fff;
  color: var(--tone-amber);
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.notice-action:disabled { opacity: 0.6; cursor: not-allowed; }
.notice-dismiss { flex: none; width: 30px; height: 30px; border: 0; border-radius: 8px; background: transparent; color: var(--tone-amber); font-size: 18px; cursor: pointer; }

.thread { display: flex; flex: 1; flex-direction: column; gap: 14px; overflow-y: auto; padding: 18px 16px; background: #fcfbfa; scroll-behavior: smooth; }
.welcome { display: flex; flex-direction: column; gap: 12px; }
.welcome h2 { margin: 0; font-size: 18px; }
.welcome p { margin: 0; color: var(--app-text-body); font-size: 13px; line-height: 1.75; }
.label { color: var(--app-text-body); font-size: 12px; font-weight: 600; letter-spacing: 1px; }
.questions { display: flex; flex-direction: column; gap: 8px; }
.question {
  min-height: 44px;
  padding: 0 14px;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: var(--app-surface);
  color: var(--app-text);
  font: inherit;
  font-size: 14px;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--motion-fast), transform var(--motion-fast);
}
.question:hover:not(:disabled) { border-color: var(--el-color-primary-light-5); transform: translateX(2px); }
.question:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.question:disabled { color: var(--app-text-muted); cursor: not-allowed; }
.how { display: flex; flex-direction: column; gap: 8px; padding: 14px; border: 1px solid #efe9e3; border-radius: 10px; background: var(--app-surface); }
.how ol { display: flex; flex-direction: column; gap: 6px; margin: 0; padding-left: 18px; color: var(--app-text-body); font-size: 13px; line-height: 1.6; }

.messages { display: flex; flex-direction: column; gap: 14px; }
.message { display: flex; gap: 10px; }
.message.customer { justify-content: flex-end; }
.avatar {
  display: grid;
  place-items: center;
  flex: none;
  width: 32px;
  height: 32px;
  border-radius: 10px;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 14px;
  font-weight: 700;
}
.body { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; max-width: 86%; min-width: 0; }
.customer .body { align-items: flex-end; max-width: 82%; }
.message.assistant .body { flex: 1; }
.who { color: var(--app-text-muted); font-size: 12px; }
.bubble { margin: 0; padding: 10px 14px; border-radius: 14px; font-size: 14px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.customer .bubble { border-top-right-radius: 4px; background: var(--el-color-primary); color: #fff; }
.message.assistant .bubble { border: 1px solid #ece6e0; border-top-left-radius: 4px; background: var(--app-surface); }
.pending .bubble { opacity: 0.75; }
.card { display: flex; align-items: center; gap: 10px; min-width: 220px; padding: 8px 10px; border: 1px solid #ece6e0; border-radius: 12px; background: var(--app-surface); font-size: 13px; font-weight: 600; }
.card span { display: flex; flex-direction: column; }
.card small { color: var(--app-text-muted); font-size: 12px; font-weight: 400; }
.evidence-link { padding: 0 4px; }
.message :deep(.application) { align-self: stretch; width: 100%; }
.unplaced { margin-left: 42px; }
.typing { display: inline-flex; align-items: center; gap: 5px; padding: 13px 16px; border: 1px solid #ece6e0; border-radius: 14px; border-top-left-radius: 4px; background: var(--app-surface); }
.typing i { width: 7px; height: 7px; border-radius: 4px; background: #a39a93; animation: dots 1.2s ease-in-out infinite; }
.typing i:nth-child(2) { animation-delay: 0.15s; }
.typing i:nth-child(3) { animation-delay: 0.3s; }
.busy { margin: 0; color: var(--app-text-muted); font-size: 13px; text-align: center; }
.msg-enter-active { transition: opacity var(--motion-base) ease-out, transform var(--motion-base) ease-out; }
.msg-enter-from { opacity: 0; transform: translateY(8px); }

.fab { display: none; }

@keyframes dots {
  0%, 60%, 100% { opacity: 0.5; transform: translateY(0); }
  30% { opacity: 1; transform: translateY(-4px); }
}

/* Tablets and phones: the orders are the page; the conversation opens over it. */
@media (max-width: 900px) {
  .layout, .conversing .layout { grid-template-columns: minmax(0, 1fr); padding: 16px 16px 96px; }
  .chat-panel {
    position: fixed;
    inset: 0;
    z-index: 40;
    height: auto;
    min-height: 0;
    border: 0;
    border-radius: 0;
    transform: translateX(100%);
    visibility: hidden;
    transition: transform var(--motion-slow) var(--motion-ease), visibility 0s linear var(--motion-slow);
  }
  .chat-panel.open { transform: none; visibility: visible; transition: transform var(--motion-slow) var(--motion-ease); }
  .back { display: inline-flex; }
  .unplaced { margin-left: 0; }
  .fab {
    position: fixed;
    right: 16px;
    bottom: 24px;
    z-index: 30;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    height: 52px;
    padding: 0 20px 0 16px;
    border: 0;
    border-radius: 26px;
    background: var(--el-color-primary);
    color: #fff;
    font: inherit;
    font-size: 15px;
    font-weight: 600;
    box-shadow: 0 10px 24px rgb(160 64 24 / 35%);
    cursor: pointer;
    transition: transform var(--motion-base), opacity var(--motion-base);
  }
  .fab .el-icon { font-size: 20px; }
  .fab.hidden { opacity: 0; pointer-events: none; transform: translateY(12px); }
  .fab-badge { padding: 2px 8px; border-radius: 10px; background: #fff; color: var(--el-color-primary-dark-2); font-size: 12px; }
}
</style>
