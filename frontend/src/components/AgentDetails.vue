<script setup>
import { computed } from 'vue'

const props = defineProps({
  citations: { type: Array, default: () => [] },
  trace: { type: Object, default: null },
  action: { type: Object, default: null },
})

const steps = computed(() => Array.isArray(props.trace?.steps) ? props.trace.steps : [])
const modelCalls = computed(() => Array.isArray(props.trace?.model_calls) ? props.trace.model_calls : [])
const isKnowledge = (citation) => citation.producer === 'search_knowledge_base'
const citationTitle = (citation) => isKnowledge(citation) ? (citation.title || citation.doc_id || '售后知识库') : citation.ref
const actionNames = { create_return: '退货', create_exchange: '换货', escalate_to_human: '转人工' }
const stepTitles = {
  clarify: '补充信息',
  finish: '生成回复',
  step_limit: '本轮步骤已达上限',
  grounding_rejected: '未通过订单核对',
}
const outcomeTone = computed(() => {
  if (props.action?.status === 'EXECUTED') return 'executed'
  if (props.action?.status === 'WAITING_APPROVAL') return 'pending'
  return 'stopped'
})
</script>

<template>
  <div v-if="citations.length || trace" class="agent-details">
    <details v-if="citations.length" class="detail-panel">
      <summary>依据 <span class="detail-count">{{ citations.length }}</span></summary>
      <ul class="citation-list">
        <li v-for="(citation, index) in citations" :key="`${citation.ref}-${index}`" class="citation-item">
          <strong>{{ citationTitle(citation) }}</strong>
          <div class="citation-meta">
            <template v-if="isKnowledge(citation)">
              <span>知识库</span>
              <span v-if="citation.version">版本 {{ citation.version }}</span>
              <span v-if="citation.section">{{ citation.section }}</span>
            </template>
            <template v-else>
              <span v-if="citation.producer">{{ citation.producer }}</span>
              <span v-if="citation.source_type">{{ citation.source_type }}</span>
            </template>
          </div>
          <p v-if="isKnowledge(citation) && citation.doc_id" class="citation-locator">{{ citation.doc_id }}</p>
          <p v-else-if="citation.locator" class="citation-locator">{{ citation.locator }}</p>
        </li>
      </ul>
    </details>

    <details v-if="trace" class="detail-panel">
      <summary>Agent Trace <span class="detail-count">{{ steps.length }} 步</span></summary>
      <ol v-if="steps.length || action" class="trace-list">
        <li v-for="(step, index) in steps" :key="`${step.run}-${step.step}-${index}`" class="trace-step">
          <div v-if="step.run != null || step.step != null" class="trace-position">
            <span v-if="step.run != null">Run {{ step.run }}</span>
            <span v-if="step.run != null && step.step != null" aria-hidden="true"> / </span>
            <span v-if="step.step != null">Step {{ step.step }}</span>
          </div>
          <div class="trace-title">
            <template v-if="step.kind === 'tool_call'">
              <code>{{ step.tool_name }}</code>
              <span v-if="step.result_status" class="trace-result">{{ step.result_status }}</span>
            </template>
            <template v-else-if="step.kind === 'action_proposed'">
              <span>Action proposed</span>
              <span class="trace-action">{{ actionNames[step.action_name] || step.action_name }}</span>
            </template>
            <span v-else>{{ stepTitles[step.kind] || step.kind }}</span>
          </div>
          <code v-if="step.kind === 'action_proposed' && step.action_name" class="trace-secondary">{{ step.action_name }}</code>
          <code v-if="step.kind === 'grounding_rejected' && step.code" class="trace-secondary">{{ step.code }}</code>
          <span v-if="step.observation_id" class="trace-secondary">{{ step.observation_id }}</span>
          <span v-if="step.slots?.length" class="trace-secondary">{{ step.slots.join(' · ') }}</span>
          <span v-if="step.disposition" class="trace-secondary">{{ step.disposition }}</span>
        </li>
        <li v-if="action" class="trace-step trace-outcome" :class="outcomeTone">
          <span class="trace-position">Guard / outcome</span>
          <div class="trace-title">
            <span v-if="action.guard?.decision">{{ action.guard.decision }}</span>
            <span v-if="action.status" class="outcome-label">{{ action.status }}</span>
          </div>
          <code v-if="action.guard?.reason_code" class="trace-secondary">{{ action.guard.reason_code }}</code>
          <code v-if="action.code && action.code !== action.guard?.reason_code" class="trace-secondary">{{ action.code }}</code>
        </li>
      </ol>
      <p v-else class="trace-empty">本次响应未包含步骤记录。</p>
      <ol v-if="modelCalls.length" class="model-call-list" aria-label="模型调用用量">
        <li v-for="(call, index) in modelCalls" :key="index">
          <strong>{{ call.kind === 'generation' ? '回复生成' : '处理决策' }} {{ index + 1 }}</strong>
          <span>输入 {{ call.prompt_tokens ?? '—' }} · 输出 {{ call.completion_tokens ?? '—' }} tokens</span>
          <span v-if="call.latency_seconds != null">{{ Number(call.latency_seconds).toFixed(2) }} 秒</span>
          <span v-if="call.status && call.status !== 'success'">{{ call.status === 'provider_error' ? '调用失败' : call.status === 'protocol_error' ? '回复校验失败' : call.status }}</span>
        </li>
      </ol>
    </details>
  </div>
</template>

<style scoped>
/* Light theme: shown inside the customer's 处理依据 drawer (技术细节). */
.agent-details { display: grid; gap: 8px; min-width: 0; }
.detail-panel { min-width: 0; border: 1px solid #efe9e3; border-radius: 10px; background: #fff; }
.detail-panel summary { padding: 10px 13px; color: #3a332e; cursor: pointer; font-size: 13px; font-weight: 600; }
.detail-panel summary::marker { color: #c8501e; }
.detail-panel summary:hover { color: #a04018; }
.detail-panel summary:focus-visible { outline: 2px solid #c8501e; outline-offset: 3px; border-radius: 8px; }
.detail-count { margin-left: 7px; color: #6b625c; font-size: 12px; font-weight: 400; }
.citation-list { display: grid; gap: 8px; margin: 0; padding: 0 13px 13px; list-style: none; }
.model-call-list { display: grid; gap: 7px; margin: 0; padding: 0 13px 13px; list-style: none; font-size: 12px; color: #6b625c; }
.model-call-list li { display: flex; flex-wrap: wrap; gap: 5px 12px; padding-top: 8px; border-top: 1px solid #f3eee9; }
.model-call-list strong { color: #3a332e; font-weight: 600; }
.citation-item { min-width: 0; padding: 10px 12px; border-radius: 8px; background: #faf8f6; overflow-wrap: anywhere; }
.citation-item strong { font-size: 13px; color: #1f1a17; }
.citation-meta { display: flex; flex-wrap: wrap; gap: 5px 12px; margin-top: 4px; color: #6b625c; font-size: 12px; }
.citation-locator { margin: 6px 0 0; color: #4a423d; font-size: 12px; white-space: pre-wrap; }
.trace-list { margin: 0; padding: 3px 14px 14px 23px; list-style: none; }
.trace-step { position: relative; min-width: 0; padding: 0 0 18px 19px; border-left: 2px solid #ece6e0; overflow-wrap: anywhere; }
.trace-step::before { position: absolute; top: 5px; left: -5px; width: 8px; height: 8px; content: ''; border-radius: 50%; background: #c8501e; box-shadow: 0 0 0 3px #fff; }
.trace-step:last-child { padding-bottom: 0; border-left-color: transparent; }
.trace-position { display: block; margin-bottom: 4px; color: #6b625c; font-size: 11px; letter-spacing: .04em; }
.trace-title { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; color: #1f1a17; font-size: 13px; line-height: 1.6; }
.trace-title code { font-size: 12px; color: #a04018; }
.trace-result, .trace-action { padding: 1px 6px; border-radius: 4px; background: #f0ece8; color: #514a45; font-size: 11px; }
.trace-secondary { display: block; margin-top: 5px; color: #6b625c; font-size: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.outcome-label { font-size: 11px; padding: 1px 6px; border-radius: 4px; }
.executed .outcome-label { background: #e6f4ec; color: #1a6b43; }
.pending .outcome-label { background: #fff3dc; color: #7a4a00; }
.stopped .outcome-label { background: #fdecea; color: #a61f15; }
.trace-empty { margin: 0; padding: 0 13px 13px; color: #6b625c; font-size: 12px; }
</style>
