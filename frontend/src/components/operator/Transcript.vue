<script setup lang="ts">
// The middle column: the session's conversation as the server keeps it. A
// reply that proposed or settled an action links to that action in 审批详情.
import { computed, nextTick, ref, watch } from 'vue'

import StatusTag from '@/components/StatusTag.vue'
import type { ActionView, SessionView } from '@/types'
import { actionTitle, SESSION_LABELS } from '@/utils/desk'
import { shortId } from '@/utils/format'

const props = withDefaults(defineProps<{
  view: SessionView | null
  /** The action shown in 审批详情; its messages are marked. */
  focusId?: string | null
  loading?: boolean
}>(), { focusId: null, loading: false })

const emit = defineEmits<{ focus: [pendingActionId: string] }>()

const body = ref<HTMLElement | null>(null)
const status = computed(() => props.view ? SESSION_LABELS[props.view.status] ?? { text: props.view.status, tone: 'gray' as const } : null)
const actionsById = computed(() => new Map((props.view?.pending_actions ?? [])
  .filter((action) => action.pending_action_id).map((action) => [action.pending_action_id!, action])))

function label(kind: string | undefined, action: ActionView | undefined) {
  if (kind === 'operator_decision') return '智能客服 · 审批结果'
  if (kind === 'action' && action) return `智能客服 · 提出${actionTitle(action)}`
  if (kind === 'clarification') return '智能客服 · 追问'
  return '智能客服'
}

const entries = computed(() => (props.view?.messages ?? []).map((message, index) => {
  const action = message.pending_action_id ? actionsById.value.get(message.pending_action_id) : undefined
  return {
    key: `${index}-${message.role}`,
    message,
    action,
    who: message.role === 'customer' ? props.view!.persona.display_name : label(message.kind, action),
    decision: message.kind === 'operator_decision',
  }
}))

// A newly selected session opens on its latest message.
watch(() => props.view?.session_id, async () => {
  await nextTick()
  if (body.value) body.value.scrollTop = body.value.scrollHeight
})
</script>

<template>
  <section class="transcript" aria-label="对话记录">
    <template v-if="view">
      <header class="head">
        <strong>{{ view.persona.display_name }}</strong>
        <StatusTag v-if="status" :tone="status.tone" :pulse="view.status === 'WAITING_APPROVAL'">{{ status.text }}</StatusTag>
        <span class="mono" :title="view.session_id">{{ shortId(view.session_id) }}</span>
      </header>
      <div ref="body" class="body" :aria-busy="loading">
        <p v-if="!entries.length" class="empty">顾客还没有发消息。</p>
        <template v-for="{ key, message, action, who, decision } in entries" :key="key">
          <div v-if="decision" class="divider">审批结果</div>
          <article class="message" :class="[message.role, { marked: focusId && message.pending_action_id === focusId }]">
            <span class="avatar" aria-hidden="true">{{ message.role === 'customer' ? [...view.persona.display_name].pop() : 'G' }}</span>
            <div class="column">
              <span class="who">{{ who }}</span>
              <p class="bubble">{{ message.text }}</p>
              <button v-if="action && message.pending_action_id !== focusId" type="button" class="link"
                @click="emit('focus', message.pending_action_id!)">查看这笔申请</button>
            </div>
          </article>
        </template>
      </div>
    </template>
    <div v-else class="placeholder">
      <p>{{ loading ? '正在读取会话…' : '在左侧选择一个会话，查看对话和待审批的申请。' }}</p>
    </div>
  </section>
</template>

<style scoped>
.transcript { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: #fcfbfa; }
.head { display: flex; align-items: center; gap: 12px; padding: 14px 24px; background: var(--app-surface); border-bottom: 1px solid #efe9e3; flex: none; }
.head strong { font-size: 16px; }
.mono { color: var(--app-text-muted); font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; }
.body { display: flex; flex: 1; flex-direction: column; gap: 14px; min-height: 0; overflow-y: auto; padding: 20px 24px; }
.divider { display: flex; align-items: center; gap: 10px; color: var(--app-text-muted); font-size: 12px; }
.divider::before, .divider::after { flex: 1; height: 1px; content: ''; background: #ece6e0; }
.message { display: flex; gap: 10px; max-width: 78%; animation: app-rise var(--motion-base) ease-out both; }
.message.assistant { flex-direction: row-reverse; align-self: flex-end; }
.avatar { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 10px; font-size: 13px; font-weight: 700; flex: none; }
.customer .avatar { background: var(--tone-peach-bg); color: var(--el-color-primary-dark-2); }
.assistant .avatar { background: #1f1a17; color: #fff; }
.column { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.assistant .column { align-items: flex-end; }
.who { color: var(--app-text-muted); font-size: 12px; }
.bubble { margin: 0; padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.7; white-space: pre-wrap; overflow-wrap: anywhere; }
.customer .bubble { background: var(--app-surface); border: 1px solid #ece6e0; border-top-left-radius: 4px; }
.assistant .bubble { background: #f3efeb; border: 1px solid #ebe4dd; border-top-right-radius: 4px; }
.marked .bubble { border-color: #efcbbc; box-shadow: 0 0 0 3px rgb(200 80 30 / 8%); }
.link { padding: 0; border: 0; background: none; color: var(--el-color-primary-dark-2); font: inherit; font-size: 12px; cursor: pointer; }
.link:hover { text-decoration: underline; }
.link:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.empty { margin: auto; color: var(--app-text-muted); font-size: 14px; }
.placeholder { display: grid; flex: 1; place-items: center; padding: 40px; color: var(--app-text-muted); font-size: 14px; line-height: 1.8; text-align: center; }
</style>
