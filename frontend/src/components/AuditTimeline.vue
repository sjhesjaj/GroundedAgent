<script setup>
defineProps({ events: { type: Array, default: () => [] } })

const fields = ['phase', 'decision', 'code', 'pending_action_id', 'receipt_id', 'approver_ref']
const eventLabels = {
  'guard.evaluated': 'Guard 校验',
  'action.pending_created': '已创建待审批操作',
  'approval.recorded': '人工决定已记录',
  'resume.started': '开始恢复执行',
  'resume.version_check': '重新校验业务状态',
  'action.executed': '操作已执行',
  'action.not_executed': '操作未执行',
  'action.replay_hit': '返回已有结果',
  'approval.conflict': '审批决定冲突',
}
</script>

<template>
  <details v-if="events.length" class="audit-panel">
    <summary>审计记录 <span>{{ events.length }} 条事件</span></summary>
    <ol class="audit-list">
      <li v-for="(event, index) in events" :key="event.event_seq ?? index" class="audit-event">
        <div class="audit-heading">
          <strong>{{ eventLabels[event.event_name] || event.event_name }}</strong>
          <time v-if="event.at" :datetime="event.at">{{ event.at }}</time>
        </div>
        <code class="event-name">{{ event.event_name }}</code>
        <dl class="audit-fields">
          <template v-for="field in fields" :key="field">
            <div v-if="event[field] != null && event[field] !== ''" class="audit-field">
              <dt>{{ field }}</dt>
              <dd>{{ event[field] }}</dd>
            </div>
          </template>
        </dl>
      </li>
    </ol>
  </details>
</template>

<style scoped>
.audit-panel { min-width: 0; margin-top: 20px; border: 1px solid #ffffff13; border-radius: 13px; background: #0b1512; }
.audit-panel summary { padding: 13px 15px; cursor: pointer; color: #b7ccc1; font-size: 12px; font-weight: 600; }
.audit-panel summary::marker { color: #7ee2b8; }
.audit-panel summary:hover { color: #7ee2b8; }
.audit-panel summary:focus-visible { outline: 2px solid #7ee2b8; outline-offset: 3px; border-radius: 9px; }
.audit-panel summary > span { margin-left: 8px; color: #91a69c; font-size: 11px; font-weight: 400; }
.audit-list { margin: 0; padding: 5px 17px 18px 26px; list-style: none; }
.audit-event { position: relative; min-width: 0; padding: 0 0 22px 19px; border-left: 1px solid #7ee2b82e; overflow-wrap: anywhere; }
.audit-event::before { position: absolute; top: 6px; left: -4px; width: 7px; height: 7px; content: ''; border-radius: 50%; background: #6d9783; box-shadow: 0 0 0 4px #0b1512; }
.audit-event:last-child { padding-bottom: 0; border-left-color: transparent; }
.audit-heading { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 5px 14px; }
.audit-heading strong { color: #d0dfd7; font-size: 12px; font-weight: 600; }
.audit-heading time { color: #91a69c; font-size: 10px; }
.event-name { display: block; margin-top: 4px; color: #a8c4b6; font-size: 11px; overflow-wrap: anywhere; }
.audit-fields { display: grid; gap: 5px; margin: 10px 0 0; }
.audit-fields:empty { display: none; }
.audit-field { display: grid; grid-template-columns: 122px minmax(0, 1fr); gap: 5px 10px; font-size: 11px; line-height: 1.6; }
.audit-field dt { color: #7f988b; }
.audit-field dd { min-width: 0; margin: 0; color: #bccdc4; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 480px) {
  .audit-list { padding-right: 13px; padding-left: 20px; }
  .audit-field { grid-template-columns: minmax(0, 1fr); gap: 0; }
}
</style>
