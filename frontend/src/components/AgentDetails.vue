<script setup>
import { computed } from 'vue'

const props = defineProps({
  citations: { type: Array, default: () => [] },
  trace: { type: Object, default: null },
  action: { type: Object, default: null },
})

const steps = computed(() => Array.isArray(props.trace?.steps) ? props.trace.steps : [])
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
          <strong>{{ citation.ref }}</strong>
          <div class="citation-meta">
            <span v-if="citation.producer">{{ citation.producer }}</span>
            <span v-if="citation.source_type">{{ citation.source_type }}</span>
          </div>
          <p v-if="citation.locator" class="citation-locator">{{ citation.locator }}</p>
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
    </details>
  </div>
</template>

<style scoped>
.agent-details { display: grid; gap: 8px; margin-top: 15px; min-width: 0; }
.detail-panel { min-width: 0; border: 1px solid #ffffff10; border-radius: 11px; background: #0b1512; }
.detail-panel summary { padding: 10px 13px; color: #aec6bb; cursor: pointer; font-size: 12px; font-weight: 600; }
.detail-panel summary::marker { color: #7ee2b8; }
.detail-panel summary:hover { color: #7ee2b8; }
.detail-panel summary:focus-visible { outline: 2px solid #7ee2b8; outline-offset: 3px; border-radius: 8px; }
.detail-count { margin-left: 7px; color: #91a69c; font-size: 11px; font-weight: 400; }
.citation-list { display: grid; gap: 8px; margin: 0; padding: 0 13px 13px; list-style: none; }
.citation-item { min-width: 0; padding: 10px 12px; border-left: 2px solid #7ee2b855; border-radius: 0 7px 7px 0; background: #ffffff03; overflow-wrap: anywhere; }
.citation-item strong { font-size: 12px; color: #d0e0d8; }
.citation-meta { display: flex; flex-wrap: wrap; gap: 5px 12px; margin-top: 4px; color: #91a69c; font-size: 11px; }
.citation-locator { margin: 6px 0 0; color: #aec6bb; font-size: 12px; white-space: pre-wrap; }
.trace-list { margin: 0; padding: 3px 14px 14px 23px; list-style: none; }
.trace-step { position: relative; min-width: 0; padding: 0 0 21px 19px; border-left: 1px solid #7ee2b82e; overflow-wrap: anywhere; }
.trace-step::before { position: absolute; top: 5px; left: -4px; width: 7px; height: 7px; content: ''; border-radius: 50%; background: #7ee2b8; box-shadow: 0 0 0 4px #0b1512; }
.trace-step:not(:last-child)::after { position: absolute; bottom: 1px; left: -5px; content: '↓'; color: #6e9e87; font-size: 12px; line-height: 15px; }
.trace-step:last-child { padding-bottom: 0; border-left-color: transparent; }
.trace-position { display: block; margin-bottom: 4px; color: #91a69c; font-size: 10px; letter-spacing: .04em; }
.trace-title { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; color: #d0e0d8; font-size: 12px; line-height: 1.6; }
.trace-title code { font-size: 12px; color: #b7e4cf; }
.trace-result, .trace-action { padding: 1px 6px; border: 1px solid #ffffff12; border-radius: 5px; color: #91a69c; font-size: 10px; }
.trace-secondary { display: block; margin-top: 5px; color: #91a69c; font-size: 11px; white-space: pre-wrap; overflow-wrap: anywhere; }
.outcome-label { font-size: 10px; padding: 2px 6px; border: 1px solid currentColor; border-radius: 5px; }
.executed .outcome-label { color: #7ee2b8; }
.pending .outcome-label { color: #e7bd77; }
.stopped .outcome-label { color: #e79b93; }
.trace-empty { margin: 0; padding: 0 13px 13px; color: #91a69c; font-size: 12px; }
</style>
