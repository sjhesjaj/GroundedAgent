<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { aftersalesApi } from './api'
import { openSession, rememberSession } from './sessionMemory'
import ActionCard from './components/ActionCard.vue'
import AgentDetails from './components/AgentDetails.vue'
import AuditTimeline from './components/AuditTimeline.vue'

const demo = ref(null)
const selectedPersonaId = ref('')
const session = ref(null)
const messages = ref([])
const actions = ref({})
const audit = ref([])
const question = ref('')
const busy = ref('startup')
const decidingId = ref('')
const notice = ref('')
const invalidSession = ref(false)
const syncRequired = ref(false)
const messageList = ref(null)
const composer = ref(null)
let messageSequence = 0

const suggestions = [
  { label: '查询订单', text: 'ORD-1001 现在是什么状态？', icon: '01' },
  { label: '申请退货', text: '我要退 ORD-1001 里的内衣，不想要了', icon: '02' },
  { label: '咨询换货', text: 'ORD-1001 的商品可以换货吗？', icon: '03' },
  { label: '人工协助', text: '我想转人工处理这个售后问题', icon: '04' },
]
const statusLabels = { OPEN: '会话进行中', NEEDS_CLARIFICATION: '等待补充信息', WAITING_APPROVAL: '等待人工审批' }
const locked = computed(() => Boolean(busy.value))
const canInteract = computed(() => Boolean(session.value) && !invalidSession.value && !syncRequired.value && !locked.value)
const canSend = computed(() => canInteract.value && question.value.trim().length > 0 && question.value.trim().length <= 2000)
const sessionStatus = computed(() => invalidSession.value ? '会话已失效' : (syncRequired.value ? '等待同步状态' : statusLabels[session.value?.status] || '尚未连接'))
const businessTime = computed(() => (session.value?.business_time || demo.value?.business_time || '').replace('T', ' '))
const unplacedActions = computed(() => Object.entries(actions.value).filter(([key]) => !messages.value.some((message) => message.actionKey === key)))

async function scrollToEnd() {
  await nextTick()
  if (window.matchMedia('(max-width: 760px)').matches) {
    messageList.value?.lastElementChild?.scrollIntoView({ block: 'end' })
  } else messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'auto' })
}

function updateHeader(data) {
  session.value = {
    session_id: data.session_id, persona: data.persona, business_time: data.business_time,
    status: data.status, pending_action_id: data.pending_action_id,
  }
  selectedPersonaId.value = data.persona.persona_id
}

// GET /sessions owns the transcript; rich metadata survives only when the
// corresponding entry still matches, since the server does not persist trace.
function reconcile(data) {
  const previous = messages.value
  updateHeader(data)
  for (const action of data.pending_actions || []) actions.value[action.pending_action_id] = action
  const placed = new Set()
  messages.value = data.messages.map((entry, index) => {
    const old = previous[index]
    const same = old?.role === entry.role && old?.text === entry.text
    const item = { ...(same ? old : {}), ...entry, id: same ? old.id : ++messageSequence }
    const key = entry.pending_action_id || (same ? old.actionKey : null)
    item.actionKey = entry.role === 'assistant' && key && !placed.has(key) ? key : null
    if (item.actionKey) placed.add(key)
    return item
  })
  audit.value = data.audit || []
  invalidSession.value = false
  syncRequired.value = false
}

function activate(data) {
  messages.value = []
  actions.value = {}
  audit.value = []
  question.value = ''
  reconcile(data)
  // Survives a page reload; the server keeps the session across restarts.
  rememberSession(data.session_id)
}

function applyResponse(data, customerText) {
  updateHeader(data)
  if (customerText !== undefined) messages.value.push({ id: ++messageSequence, role: 'customer', text: customerText })
  const actionKey = data.action ? (data.action.pending_action_id || `turn-${++messageSequence}`) : null
  if (actionKey) actions.value[actionKey] = data.action
  // Replays/conflicts return a reply but do not append to the server transcript.
  const replayedDecision = data.operator_decision && (data.action?.idempotent_replay || data.action?.decision_conflict)
  if (replayedDecision) notice.value = data.reply.text
  else messages.value.push({
    id: ++messageSequence, role: 'assistant', text: data.reply.text, kind: data.reply.kind,
    citations: data.citations, trace: data.trace, traceAction: data.action,
    pending_action_id: data.action?.pending_action_id,
    actionKey: actionKey && !messages.value.some((message) => message.actionKey === actionKey) ? actionKey : null,
  })
  audit.value = data.audit || []
}

function showError(error) {
  notice.value = error.message
  if (error.code === 'session_not_found') invalidSession.value = true
}

async function loadDemo() {
  demo.value = await aftersalesApi.demo()
  if (!demo.value.personas.some((persona) => persona.persona_id === selectedPersonaId.value)) {
    selectedPersonaId.value = demo.value.personas[0]?.persona_id || ''
  }
  if (!selectedPersonaId.value) throw new Error('Demo 暂无可用客户，请检查后端服务。')
}

async function newSession(personaId = selectedPersonaId.value) {
  if (locked.value) return
  busy.value = 'session'
  notice.value = ''
  try {
    if (!demo.value || !personaId) {
      await loadDemo()
      personaId = selectedPersonaId.value
    }
    activate(await aftersalesApi.createSession(personaId))
  } catch (error) {
    showError(error)
  } finally {
    busy.value = ''
  }
}

async function changePersona(event) {
  const personaId = event.target.value
  // Keep the bound selection until the new session actually succeeds.
  event.target.value = selectedPersonaId.value
  await newSession(personaId)
}

async function resetDemo() {
  if (locked.value) return
  busy.value = 'reset'
  notice.value = ''
  try {
    await aftersalesApi.resetDemo()
    // Reset has invalidated the old session even if the following read fails.
    session.value = null
    messages.value = []
    actions.value = {}
    audit.value = []
    question.value = ''
    syncRequired.value = false
    invalidSession.value = false
    await loadDemo()
    activate(await aftersalesApi.createSession(selectedPersonaId.value))
  } catch (error) {
    showError(error)
    if (session.value) await recoverSession()
  } finally {
    busy.value = ''
  }
}

async function recoverSession() {
  syncRequired.value = true
  try {
    reconcile(await aftersalesApi.session(session.value.session_id))
  } catch (error) {
    if (error.code === 'session_not_found') showError(error)
    else notice.value += ' 无法同步会话，请先刷新状态再继续。'
  }
}

async function refreshSession() {
  if (locked.value || !session.value) return
  busy.value = 'sync'
  notice.value = ''
  await recoverSession()
  busy.value = ''
}

async function send() {
  if (!canSend.value) return
  const text = question.value.trim()
  busy.value = 'message'
  notice.value = ''
  try {
    applyResponse(await aftersalesApi.sendMessage(session.value.session_id, text), text)
    question.value = ''
  } catch (error) {
    showError(error)
    // An interrupted response may already have committed an action. Read its
    // persisted outcome instead of automatically retrying the mutation.
    if (!error.status || error.status >= 500 || error.code === 'invalid_response') {
      const oldCount = messages.value.length
      await recoverSession()
      if (messages.value.slice(oldCount).some((entry) => entry.role === 'customer' && entry.text === text)) question.value = ''
    }
  } finally {
    busy.value = ''
    await scrollToEnd()
    composer.value?.focus()
  }
}

async function decide(action, decision) {
  if (!canInteract.value || action.status !== 'WAITING_APPROVAL' || !action.pending_action_id) return
  busy.value = 'decision'
  decidingId.value = action.pending_action_id
  notice.value = ''
  try {
    const result = await aftersalesApi.decide(session.value.session_id, action.pending_action_id, decision)
    applyResponse(result)
    // A failed approval transaction can leave the persisted action pending.
    // Keep the failure reply, then restore the current outcome before retrying.
    if (result.action?.status === 'FAILED' || result.action?.decision_conflict) await recoverSession()
  } catch (error) {
    showError(error)
    if (error.code !== 'session_not_found') await recoverSession()
  } finally {
    busy.value = ''
    decidingId.value = ''
    await scrollToEnd()
  }
}

async function chooseSuggestion(text) {
  if (!canInteract.value) return
  question.value = text
  await nextTick()
  composer.value?.focus()
}

function composerKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault()
    send()
  }
}

onMounted(async () => {
  try {
    await loadDemo()
    // The remembered session if the server still has it, else a new one.
    activate((await openSession(aftersalesApi, selectedPersonaId.value)).data)
  } catch (error) {
    showError(error)
  } finally {
    busy.value = ''
  }
})
</script>

<template>
  <main class="shell">
    <aside class="sidebar" aria-label="演示控制台">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">G<span></span></span>
        <span><strong>GroundedAgent</strong><small>电商售后客服</small></span>
      </div>
      <div class="sidebar-label">DEMO WORKSPACE <span>M0</span></div>
      <section class="panel persona-panel">
        <label class="section-title" for="persona">演示客户 <span>Demo customer</span></label>
        <div class="customer-icon" aria-hidden="true">客</div>
        <select id="persona" :value="selectedPersonaId" :disabled="locked || !demo?.personas.length" @change="changePersona">
          <option v-if="!demo?.personas.length" value="">正在加载客户…</option>
          <option v-for="persona in demo?.personas" :key="persona.persona_id" :value="persona.persona_id">{{ persona.display_name }} · {{ persona.persona_id }}</option>
        </select>
        <small class="persona-reference">{{ selectedPersonaId }}</small>
        <p class="helper">Persona 是演示客户的替身，不代表身份认证。切换客户会新建会话。</p>
      </section>
      <section class="panel demo-panel">
        <h2 class="section-title">演示状态 <span>Demo state</span></h2>
        <dl>
          <div><dt>业务时间</dt><dd class="business-time">{{ businessTime || '—' }}</dd></div>
          <div><dt>当前会话</dt><dd class="mono" :title="session?.session_id">{{ session ? `${session.session_id.slice(0, 8)}…${session.session_id.slice(-4)}` : '—' }}</dd></div>
          <div><dt>会话状态</dt><dd class="session-state" :class="{ waiting: session?.status === 'WAITING_APPROVAL', invalid: invalidSession || syncRequired }"><i></i>{{ sessionStatus }}</dd></div>
        </dl>
      </section>
      <div class="sidebar-actions">
        <button class="primary new-session" :disabled="locked" @click="newSession()"><span aria-hidden="true">＋</span>新建会话</button>
        <button class="secondary" :disabled="locked" @click="resetDemo"><span aria-hidden="true">↻</span>重置 Demo</button>
        <small>重置将清空所有演示会话与模拟业务记录。</small>
      </div>
      <div class="demo-warning"><span aria-hidden="true">◇</span><p>本地演示环境 · 非真实支付/退款/履约系统</p></div>
    </aside>

    <section class="chat" aria-label="售后客服会话">
      <header class="chat-header">
        <div><div class="eyebrow">GROUNDED IN EVERY STEP</div><h1>电商售后客服</h1><p>订单查询、退换货与人工升级</p></div>
        <span class="header-badge"><i></i>售后服务 Demo</span>
      </header>
      <div v-if="notice" class="notice" role="alert">
        <span>{{ notice }}</span>
        <button v-if="syncRequired && !invalidSession" :disabled="locked" @click="refreshSession">刷新状态</button>
        <button v-else-if="invalidSession || !session" :disabled="locked" @click="newSession()">{{ invalidSession ? '新建会话' : '重新连接' }}</button>
        <button v-else class="dismiss" aria-label="关闭提示" @click="notice = ''">×</button>
      </div>
      <div ref="messageList" class="messages" :aria-busy="locked">
        <section v-if="!messages.length" class="welcome">
          <div class="welcome-symbol" aria-hidden="true">G<span>✓</span></div>
          <div class="eyebrow">YOUR AFTER-SALES ASSISTANT</div>
          <h2>售后问题，从这里开始。</h2>
          <p>告诉我你的订单和遇到的问题。<br>查询有依据，操作有审批，处理有记录。</p>
          <div class="welcome-steps" aria-label="服务流程"><span>查询订单与规则</span><b>→</b><span>提出售后方案</span><b>→</b><span>审批与执行</span></div>
          <div class="suggestions">
            <button v-for="item in suggestions" :key="item.label" :disabled="!canInteract" @click="chooseSuggestion(item.text)">
              <span class="suggestion-top"><span>{{ item.icon }}</span><b>{{ item.label }}</b><span aria-hidden="true">↗</span></span>
              <span class="suggestion-text">{{ item.text }}</span>
            </button>
          </div>
          <small>示例仅供开始对话，具体处理以实际查询和审核结果为准。</small>
        </section>
        <div v-else class="conversation-start"><span></span>本次会话 · {{ session?.persona.display_name }}<span></span></div>
        <article v-for="message in messages" :key="message.id" class="message" :class="message.role">
          <div class="avatar" aria-hidden="true">{{ message.role === 'customer' ? '客' : 'G' }}</div>
          <div class="message-body">
            <div class="message-label">{{ message.role === 'customer' ? session?.persona.display_name : 'GroundedAgent' }}<span v-if="message.kind === 'operator_decision'">审批结果</span></div>
            <p class="message-text">{{ message.text }}</p>
            <AgentDetails v-if="message.role === 'assistant'" :citations="message.citations" :trace="message.trace" :action="message.traceAction" />
            <ActionCard v-if="message.actionKey && actions[message.actionKey]" :action="actions[message.actionKey]" :disabled="!canInteract" :deciding="decidingId === actions[message.actionKey].pending_action_id" @decide="decide(actions[message.actionKey], $event)" />
          </div>
        </article>
        <ActionCard v-for="[key, action] in unplacedActions" :key="key" :action="action" :disabled="!canInteract" :deciding="decidingId === action.pending_action_id" @decide="decide(action, $event)" />
        <AuditTimeline :events="audit" />
        <div v-if="busy" class="processing" role="status"><i></i>{{ busy === 'message' || busy === 'decision' ? '正在处理…' : busy === 'reset' ? '正在重置 Demo…' : busy === 'sync' ? '正在同步会话…' : '正在准备会话…' }}</div>
      </div>
      <footer class="chat-footer">
        <form class="composer" @submit.prevent="send">
          <label class="sr-only" for="message">售后问题</label>
          <textarea id="message" ref="composer" v-model="question" rows="2" maxlength="2000" :disabled="!canInteract" placeholder="输入订单号或描述你的售后问题…" @keydown="composerKeydown"></textarea>
          <button class="primary" type="submit" :disabled="!canSend">{{ busy === 'message' ? '正在处理…' : '发送' }}<span v-if="busy !== 'message'" aria-hidden="true">↑</span></button>
        </form>
        <div class="composer-hint"><span>Enter 发送 · Shift + Enter 换行</span><span>{{ question.length }} / 2000</span></div>
      </footer>
    </section>
  </main>
</template>
