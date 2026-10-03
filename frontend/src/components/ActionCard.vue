<script setup>
import { computed } from 'vue'

const props = defineProps({
  action: { type: Object, required: true },
  disabled: { type: Boolean, default: false },
  deciding: { type: Boolean, default: false },
})

defineEmits(['decide'])

const actionNames = {
  create_return: '退货',
  create_exchange: '换货',
  escalate_to_human: '转人工',
}

const statusNames = {
  WAITING_APPROVAL: '待人工审批',
  DENIED: '策略拒绝',
  EXECUTED: '已执行',
  REJECTED: '审批已拒绝',
  STALE: '已失效',
  FAILED: '执行失败',
}

const actionTitle = computed(() => actionNames[props.action.action_name] || props.action.action_name || '售后操作')
const statusTitle = computed(() => statusNames[props.action.status] || props.action.status)
const needsApproval = computed(() => props.action.status === 'WAITING_APPROVAL' && Boolean(props.action.pending_action_id))
const argumentEntries = computed(() => Object.entries(props.action.arguments || {}))

function formatValue(value) {
  return typeof value === 'string' ? value : JSON.stringify(value)
}
</script>

<template>
  <article class="action-card" :data-status="action.status" :aria-label="`${actionTitle} · ${statusTitle}`">
    <header class="action-header">
      <div class="action-heading">
        <span class="action-icon" aria-hidden="true">↗</span>
        <div>
          <h3>{{ actionTitle }}</h3>
          <code class="action-name">{{ action.action_name }}</code>
        </div>
      </div>
      <div class="action-status">
        <span><i aria-hidden="true"></i>{{ statusTitle }}</span>
        <code>{{ action.status }}</code>
      </div>
    </header>

    <section class="action-section">
      <h4>已校验参数</h4>
      <dl v-if="argumentEntries.length" class="action-fields">
        <div v-for="[key, value] in argumentEntries" :key="key">
          <dt>{{ key }}</dt>
          <dd>{{ formatValue(value) }}</dd>
        </div>
      </dl>
      <p v-else class="action-empty">未返回参数</p>
    </section>

    <section v-if="action.guard || action.code" class="action-section guard-section">
      <h4><span class="guard-marker" aria-hidden="true">◇</span> Policy Guard</h4>
      <dl class="action-fields">
        <div v-if="action.guard?.decision">
          <dt>decision</dt>
          <dd>{{ action.guard.decision }}</dd>
        </div>
        <div v-if="action.guard?.reason_code">
          <dt>reason_code</dt>
          <dd>{{ action.guard.reason_code }}</dd>
        </div>
        <div v-if="action.code">
          <dt>code</dt>
          <dd>{{ action.code }}</dd>
        </div>
      </dl>
    </section>

    <div v-if="action.pending_action_id" class="pending-reference">
      <span>pending_action_id</span>
      <code>{{ action.pending_action_id }}</code>
    </div>

    <section v-if="needsApproval" class="approval-section" aria-label="人工审批">
      <div class="approval-heading">
        <span class="approval-mark" aria-hidden="true">!</span>
        <div>
          <h4>人工审批</h4>
          <p>请核对上方操作与参数。批准后，服务端将重新校验再决定是否执行。</p>
        </div>
      </div>
      <div class="approval-controls" :aria-busy="deciding">
        <button type="button" class="approve-button" :disabled="disabled || deciding" @click="$emit('decide', 'APPROVE')">
          批准
        </button>
        <button type="button" class="reject-button" :disabled="disabled || deciding" @click="$emit('decide', 'REJECT')">
          拒绝
        </button>
        <span v-if="deciding" class="decision-progress" role="status">正在处理…</span>
      </div>
    </section>

    <section v-if="action.receipt" class="action-section receipt-section">
      <h4>执行回执</h4>
      <dl class="action-fields">
        <div v-if="action.receipt.receipt_id">
          <dt>receipt_id</dt>
          <dd>{{ action.receipt.receipt_id }}</dd>
        </div>
        <div v-if="action.receipt.resource_type">
          <dt>resource_type</dt>
          <dd>{{ action.receipt.resource_type }}</dd>
        </div>
        <div v-if="action.receipt.resource_id">
          <dt>resource_id</dt>
          <dd>{{ action.receipt.resource_id }}</dd>
        </div>
      </dl>
    </section>

    <div v-if="action.idempotent_replay || action.decision_conflict" class="action-notes">
      <p v-if="action.idempotent_replay">重复请求 · 返回已有结果</p>
      <p v-if="action.decision_conflict">审批决定冲突 · 保留首次决定</p>
    </div>
  </article>
</template>

<style scoped>
.action-card {
  --status-color: #91a69c;
  --status-tint: #91a69c12;
  margin-top: 18px;
  overflow: hidden;
  border: 1px solid #ffffff16;
  border-radius: 16px;
  background: #0b1512;
  color: #dce8e1;
}
.action-card[data-status='WAITING_APPROVAL'] { --status-color: #e8c281; --status-tint: #e8c28110; }
.action-card[data-status='DENIED'] { --status-color: #efa995; --status-tint: #efa99510; }
.action-card[data-status='EXECUTED'] { --status-color: #7ee2b8; --status-tint: #7ee2b810; }
.action-card[data-status='REJECTED'] { --status-color: #c9a6b6; --status-tint: #c9a6b610; }
.action-card[data-status='STALE'] { --status-color: #b6ade8; --status-tint: #b6ade810; }
.action-card[data-status='FAILED'] { --status-color: #ff9292; --status-tint: #ff929210; }
.action-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  padding: 18px 20px;
  border-bottom: 1px solid #ffffff0e;
  background: var(--status-tint);
}
.action-heading { display: flex; align-items: center; gap: 12px; min-width: 0; }
.action-heading > div { min-width: 0; }
.action-icon {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  border: 1px solid #ffffff16;
  border-radius: 10px;
  color: var(--status-color);
  font-size: 22px;
}
.action-heading h3 { margin: 0 0 3px; font-size: 16px; font-weight: 650; line-height: 1.4; }
.action-name { display: block; color: #91a69c; font-size: 11px; overflow-wrap: anywhere; }
.action-status { display: grid; gap: 4px; text-align: right; color: var(--status-color); }
.action-status > span { display: flex; align-items: center; justify-content: flex-end; gap: 7px; font-size: 12px; font-weight: 600; }
.action-status i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.action-status code { font-size: 10px; letter-spacing: .035em; overflow-wrap: anywhere; }
.action-section { padding: 16px 20px; }
.action-section + .action-section { border-top: 1px solid #ffffff0c; }
.action-section h4, .approval-heading h4 { margin: 0 0 10px; color: #b9cbc1; font-size: 12px; font-weight: 600; line-height: 1.5; }
.action-fields { display: grid; gap: 9px; margin: 0; font-size: 12px; line-height: 1.65; }
.action-fields > div { display: grid; grid-template-columns: 136px minmax(0, 1fr); gap: 12px; }
.action-fields dt { color: #91a69c; overflow-wrap: anywhere; }
.action-fields dd { margin: 0; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.action-empty { margin: 0; color: #91a69c; font-size: 12px; }
.guard-marker { margin-right: 5px; color: #7ee2b8; }
.pending-reference { display: flex; flex-wrap: wrap; align-items: baseline; gap: 7px 12px; padding: 0 20px 16px; color: #91a69c; font-size: 10px; overflow-wrap: anywhere; }
.pending-reference code { color: #adbeb4; font-size: 11px; }
.approval-section { margin: 0 12px 12px; padding: 16px; border: 1px solid #e8c28135; border-radius: 11px; background: #e8c28108; }
.approval-heading { display: flex; align-items: flex-start; gap: 10px; }
.approval-mark { display: grid; place-items: center; flex-shrink: 0; width: 21px; height: 21px; margin-top: 1px; border: 1px solid #e8c28155; border-radius: 50%; color: #e8c281; font-size: 12px; font-weight: 700; }
.approval-heading h4 { margin-bottom: 5px; color: #e8c281; font-size: 13px; }
.approval-heading p { margin: 0; color: #afa996; font-size: 12px; line-height: 1.75; }
.approval-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 16px; }
.approval-controls button { min-width: 100px; min-height: 42px; padding: 9px 20px; border: 1px solid transparent; border-radius: 8px; font: inherit; font-size: 13px; font-weight: 650; cursor: pointer; transition: background .15s, border-color .15s; }
.approve-button { background: #7ee2b8; color: #0b1512; }
.approve-button:hover:not(:disabled) { background: #a2edce; }
.approval-controls .reject-button { border-color: #ffffff24; background: #ffffff05; color: #d0dad4; }
.approval-controls .reject-button:hover:not(:disabled) { border-color: #efa99565; color: #efa995; background: #efa99508; }
.approval-controls button:focus-visible { outline: 2px solid #7ee2b8; outline-offset: 3px; }
.approval-controls button:disabled { opacity: .45; cursor: not-allowed; }
.decision-progress { color: #afa996; font-size: 12px; }
.receipt-section { border-top: 1px solid #ffffff0c; background: var(--status-tint); }
.receipt-section h4 { color: var(--status-color); }
.action-notes { display: grid; gap: 4px; padding: 12px 20px; border-top: 1px solid #ffffff0c; }
.action-notes p { margin: 0; color: #91a69c; font-size: 11px; line-height: 1.6; }
@media (max-width: 560px) {
  .action-header { padding: 16px; gap: 12px; }
  .action-section { padding: 14px 16px; }
  .action-fields > div { grid-template-columns: minmax(0, 1fr); gap: 2px; }
  .pending-reference { padding-left: 16px; padding-right: 16px; }
  .approval-section { padding: 14px; }
  .approval-controls button { flex: 1; }
  .decision-progress { flex-basis: 100%; }
  .action-status { text-align: left; }
  .action-status > span { justify-content: flex-start; }
}
</style>
