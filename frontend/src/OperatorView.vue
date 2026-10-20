<script setup>
// The operator's approval desk: every session of the demo, the selected one's
// transcript and actions, APPROVE / REJECT on its pending actions, and the demo
// reset. The approver recorded with a decision is the logged-in operator's
// (from the token, server-side); nothing here names it.
import { computed, onMounted, ref } from 'vue'
import { aftersalesApi } from './api'
import ActionCard from './components/ActionCard.vue'
import AuditTimeline from './components/AuditTimeline.vue'

defineProps({ user: { type: Object, required: true } })
defineEmits(['logout'])

const sessions = ref([])
const selectedId = ref('')
const view = ref(null)
const busy = ref('')
const decidingId = ref('')
const notice = ref('')

const statusLabels = { OPEN: '会话进行中', NEEDS_CLARIFICATION: '等待补充信息', WAITING_APPROVAL: '等待人工审批' }
const waitingCount = computed(() => sessions.value.reduce((total, item) => total + (item.waiting_approval?.length || 0), 0))
const ordered = computed(() => [...sessions.value].sort((a, b) => (b.waiting_approval?.length || 0) - (a.waiting_approval?.length || 0)))

async function loadSessions() {
  sessions.value = (await aftersalesApi.operatorSessions()).items
}

async function loadSelected() {
  view.value = selectedId.value ? await aftersalesApi.operatorSession(selectedId.value) : null
}

async function refresh() {
  if (busy.value) return
  busy.value = 'sync'
  notice.value = ''
  try {
    await loadSessions()
    if (selectedId.value && !sessions.value.some((item) => item.session_id === selectedId.value)) selectedId.value = ''
    await loadSelected()
  } catch (error) {
    notice.value = error.message
  } finally {
    busy.value = ''
  }
}

async function select(sessionId) {
  if (busy.value) return
  selectedId.value = sessionId
  busy.value = 'sync'
  notice.value = ''
  try {
    await loadSelected()
  } catch (error) {
    notice.value = error.message
  } finally {
    busy.value = ''
  }
}

async function decide(action, decision) {
  if (busy.value || !view.value || action.status !== 'WAITING_APPROVAL') return
  busy.value = 'decision'
  decidingId.value = action.pending_action_id
  notice.value = ''
  try {
    const result = await aftersalesApi.decide(view.value.session_id, action.pending_action_id, decision)
    notice.value = result.reply?.text || ''
  } catch (error) {
    notice.value = error.message
  } finally {
    // Read the persisted outcome either way; never retry the decision automatically.
    try {
      await loadSessions()
      await loadSelected()
    } catch (error) {
      notice.value = error.message
    }
    busy.value = ''
    decidingId.value = ''
  }
}

async function resetDemo() {
  if (busy.value) return
  busy.value = 'reset'
  notice.value = ''
  try {
    await aftersalesApi.resetDemo()
    selectedId.value = ''
    view.value = null
    await loadSessions()
  } catch (error) {
    notice.value = error.message
  } finally {
    busy.value = ''
  }
}

onMounted(refresh)
</script>

<template>
  <main class="shell">
    <aside class="sidebar" aria-label="审批台">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true">G<span></span></span>
        <span><strong>GroundedAgent</strong><small>售后审批台</small></span>
      </div>
      <section class="panel persona-panel">
        <h2 class="section-title">当前操作员 <span>Operator</span></h2>
        <div class="customer-icon" aria-hidden="true">审</div>
        <strong class="account-name">{{ user.display_name }}</strong>
        <small class="persona-reference">{{ user.username }}</small>
        <button class="secondary logout" @click="$emit('logout')">退出登录</button>
      </section>
      <section class="panel session-list" aria-label="会话列表">
        <h2 class="section-title">会话 <span>{{ waitingCount }} 个待审批</span></h2>
        <p v-if="!sessions.length" class="helper">暂无会话。顾客发起会话后会出现在这里。</p>
        <button v-for="item in ordered" :key="item.session_id" class="session-item"
                :class="{ active: item.session_id === selectedId, waiting: item.waiting_approval?.length }"
                :disabled="Boolean(busy)" @click="select(item.session_id)">
          <span>{{ item.persona?.display_name || '无法读取' }}</span>
          <small>{{ item.error || statusLabels[item.status] || item.status }} · {{ item.session_id.slice(0, 8) }}</small>
        </button>
      </section>
      <div class="sidebar-actions">
        <button class="primary" :disabled="Boolean(busy)" @click="refresh"><span aria-hidden="true">↻</span>刷新列表</button>
        <button class="secondary" :disabled="Boolean(busy)" @click="resetDemo">重置 Demo</button>
        <small>重置将清空所有演示会话与模拟业务记录。</small>
      </div>
    </aside>

    <section class="chat" aria-label="会话详情">
      <header class="chat-header">
        <div><div class="eyebrow">OPERATOR DESK</div><h1>售后审批</h1><p>核对 Agent 提出的售后动作，批准或拒绝</p></div>
        <span class="header-badge"><i></i>{{ view ? (statusLabels[view.status] || view.status) : '未选择会话' }}</span>
      </header>
      <div v-if="notice" class="notice" role="alert">
        <span>{{ notice }}</span>
        <button class="dismiss" aria-label="关闭提示" @click="notice = ''">×</button>
      </div>
      <div class="messages" :aria-busy="Boolean(busy)">
        <section v-if="!view" class="welcome">
          <h2>选择左侧的会话</h2>
          <p>待审批的会话排在最前。</p>
        </section>
        <template v-else>
          <div class="conversation-start"><span></span>{{ view.persona.display_name }} · {{ view.session_id }}<span></span></div>
          <article v-for="(message, index) in view.messages" :key="index" class="message" :class="message.role">
            <div class="avatar" aria-hidden="true">{{ message.role === 'customer' ? '客' : 'G' }}</div>
            <div class="message-body">
              <div class="message-label">{{ message.role === 'customer' ? view.persona.display_name : 'GroundedAgent' }}<span v-if="message.kind === 'operator_decision'">审批结果</span></div>
              <p class="message-text">{{ message.text }}</p>
            </div>
          </article>
          <ActionCard v-for="action in view.pending_actions" :key="action.pending_action_id" :action="action"
                      :disabled="Boolean(busy)" :deciding="decidingId === action.pending_action_id"
                      @decide="decide(action, $event)" />
          <AuditTimeline :events="view.audit" />
        </template>
        <div v-if="busy" class="processing" role="status"><i></i>{{ busy === 'decision' ? '正在处理审批…' : busy === 'reset' ? '正在重置 Demo…' : '正在同步…' }}</div>
      </div>
    </section>
  </main>
</template>
