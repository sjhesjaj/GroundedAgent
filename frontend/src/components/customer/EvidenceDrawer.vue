<script setup lang="ts">
// 处理依据: why a reply or a request came out the way it did. The rule check
// in words, the data and policy documents the reply cites, the request's
// audit trail, and, folded away, the raw ids and the agent trace.
import { Document, Files, Tickets } from '@element-plus/icons-vue'
import { computed, ref, watch } from 'vue'

import AgentDetails from '@/components/AgentDetails.vue'
import type { ActionView, AuditEvent, Citation, Trace } from '@/types'
import { clockTime, dateTime } from '@/utils/format'
import { ACTION_LABELS, actionEvents, eventTitle, guardVerdict } from '@/utils/outcome'

const props = withDefaults(defineProps<{
  action?: ActionView | null
  citations?: Citation[]
  trace?: Trace | null
  audit?: AuditEvent[]
  sessionId?: string
  /** Shown under the title, e.g. "退货申请 · 棉质内衣 L码 · 订单 ORD-1001". */
  subject?: string
}>(), { action: null, citations: () => [], trace: null, audit: () => [], sessionId: '', subject: '' })

const open = defineModel<boolean>({ required: true })
const tab = ref('rules')
watch(open, (value) => { if (value) tab.value = 'rules' })

const PRODUCERS: Record<string, string> = {
  get_order: '订单查询',
  get_logistics: '物流查询',
  get_inventory: '库存查询',
  get_after_sales_case: '售后单查询',
  get_my_pending_requests: '申请查询',
  search_after_sales_policy: '规则库',
  search_knowledge_base: '知识库',
  derived_facts: '推算',
}

const verdict = computed(() => guardVerdict(props.action))
const knowledge = computed(() => props.citations.filter((item) => item.producer === 'search_knowledge_base'))

interface Checked {
  key: string
  title: string
  detail: string
  source: string
}

const ARGUMENT_WORDS: Record<string, string> = { order_id: '订单', order_item_id: '商品行', sku: 'SKU', tracking_no: '运单' }

// A locator is `<entity>:<id>#<field>`, e.g. order_item:OI-1001-2#category.
const ENTITY_WORDS: Record<string, string> = {
  order: '订单', order_item: '商品行', logistics: '物流', policy: '售后规则', inventory: '库存', after_sales_case: '售后单',
}
const FIELD_WORDS: Record<string, string> = {
  category: '品类', status: '状态', paid_at: '付款时间', delivered_at: '签收时间', shipped_at: '发货时间',
  days_since_delivery: '签收天数', within_exchange_window: '是否在换货期内', within_return_window: '是否在退货期内',
  window_days: '期限天数', effective_from: '生效时间', effective_to: '失效时间', start_event: '起算事件',
  counting_rule: '计日规则', scope: '适用范围', priority: '优先级', rule_type: '规则类型', title: '名称',
  utc_offset: '时区', trigger: '触发条件', quantity: '数量', unit_price: '下单单价', available: '可用库存',
}

// What the reply looked at: its citations grouped per record (one row for a
// policy or an order line, listing the fields used), or else the read tools in
// its trace (an action reply cites nothing but has read the order first).
const records = computed<Checked[]>(() => {
  const cited = props.citations.filter((item) => item.producer !== 'search_knowledge_base')
  if (cited.length) {
    const groups = new Map<string, Checked & { fields: string[] }>()
    for (const item of cited) {
      const [subject, field] = (item.locator || item.ref).split('#')
      const [entity, id] = subject.includes(':') ? subject.split(/:(.*)/s) : ['', subject]
      const key = `${item.producer}|${subject}`
      const group = groups.get(key) ?? { key, title: ENTITY_WORDS[entity] ? `${ENTITY_WORDS[entity]} ${id}` : subject,
        detail: '', source: PRODUCERS[item.producer] ?? item.producer, fields: [] }
      if (field && !group.fields.includes(field)) group.fields.push(field)
      groups.set(key, group)
    }
    return [...groups.values()].map(({ fields, ...group }) => ({ ...group,
      detail: fields.map((field) => FIELD_WORDS[field] ?? field).join(' · ') || '整条记录' }))
  }
  return (props.trace?.steps ?? [])
    .filter((step) => step.kind === 'tool_call' && typeof step.tool_name === 'string' && step.tool_name !== 'search_knowledge_base')
    .map((step, index) => {
      const args = (step.arguments ?? {}) as Record<string, unknown>
      const what = Object.entries(args).map(([key, value]) => `${ARGUMENT_WORDS[key] ?? key} ${String(value)}`).join(' · ')
      const tool = String(step.tool_name)
      return { key: `${tool}-${index}`, title: what || (PRODUCERS[tool] ?? tool),
        detail: step.result_status === 'ok' ? '已读取' : String(step.result_status ?? ''), source: PRODUCERS[tool] ?? tool }
    })
})

const events = computed(() => actionEvents(props.audit, props.action))
const argumentsList = computed(() => Object.entries(props.action?.arguments ?? {}))
</script>

<template>
  <el-drawer v-model="open" class="evidence" direction="rtl" size="min(480px, 100%)" :append-to-body="true">
    <template #header>
      <div class="title">
        <h2>处理依据</h2>
        <p v-if="subject">{{ subject }}</p>
      </div>
    </template>

    <el-tabs v-model="tab" class="tabs">
      <el-tab-pane label="规则与核对" name="rules">
        <section class="block">
          <h3>规则校验结果</h3>
          <div v-if="verdict" class="verdict" :class="`tone-${verdict.tone}`">
            <strong>{{ verdict.title }}</strong>
            <p>{{ verdict.text }}</p>
            <span v-if="action?.guard?.reason_code" class="code">规则代码 <code>{{ action.guard.reason_code }}</code></span>
          </div>
          <p v-else class="empty">这条回复没有提交售后申请，不需要规则校验。</p>
        </section>

        <section class="block">
          <h3>依据的数据和规则</h3>
          <ul v-if="records.length" class="rows">
            <li v-for="item in records" :key="item.key" class="row">
              <el-icon class="row-icon" aria-hidden="true"><Tickets /></el-icon>
              <div class="row-text"><strong>{{ item.title }}</strong><span>{{ item.detail }}</span></div>
              <span class="source">{{ item.source }}</span>
            </li>
          </ul>
          <p v-else class="empty">{{ trace ? '这条回复没有引用订单数据。' : '刷新页面后只保留处理结果，引用的数据不再显示。' }}</p>
        </section>

        <section v-if="knowledge.length" class="block">
          <h3>引用的规则文档</h3>
          <ul class="rows">
            <li v-for="(item, index) in knowledge" :key="`${item.ref}-${index}`" class="row">
              <el-icon class="row-icon" aria-hidden="true"><Document /></el-icon>
              <div class="row-text">
                <strong>{{ item.title || item.doc_id || '售后知识库' }}</strong>
                <span><template v-if="item.version">版本 {{ item.version }}</template><template v-if="item.section"> · {{ item.section }}</template><template v-if="item.doc_id"> · {{ item.doc_id }}</template></span>
              </div>
              <span class="source kb">知识库</span>
            </li>
          </ul>
        </section>
      </el-tab-pane>

      <el-tab-pane label="处理记录" name="log">
        <ol v-if="events.length" class="timeline">
          <li v-for="(event, index) in events" :key="event.event_seq ?? index">
            <span class="tl-dot" aria-hidden="true"></span>
            <div class="tl-text">
              <strong>{{ eventTitle(event) }}</strong>
              <time :datetime="event.at" :title="dateTime(event.at)">{{ clockTime(event.at) }}</time>
              <code>{{ event.event_name }}</code>
            </div>
          </li>
        </ol>
        <p v-else class="empty">还没有处理记录。提交售后申请后，规则校验、审核和执行都会记在这里。</p>
      </el-tab-pane>

      <el-tab-pane label="技术细节" name="tech">
        <dl class="facts">
          <template v-if="sessionId"><dt>会话</dt><dd><code>{{ sessionId }}</code></dd></template>
          <template v-if="action">
            <dt>申请</dt><dd>{{ ACTION_LABELS[action.action_name] ?? action.action_name }} · <code>{{ action.action_name }}</code></dd>
            <template v-if="action.pending_action_id"><dt>申请编号</dt><dd><code>{{ action.pending_action_id }}</code></dd></template>
            <dt>状态</dt><dd><code>{{ action.status }}</code></dd>
            <template v-if="argumentsList.length">
              <dt>参数</dt>
              <dd><code v-for="[key, value] in argumentsList" :key="key" class="arg">{{ key }}={{ value }}</code></dd>
            </template>
            <template v-if="action.guard">
              <dt>Guard</dt><dd><code>{{ action.guard.decision }}</code><code v-if="action.guard.reason_code"> · {{ action.guard.reason_code }}</code></dd>
            </template>
            <template v-if="action.receipt">
              <dt>回执</dt><dd><code>{{ action.receipt.receipt_id }}</code></dd>
              <dt>{{ action.receipt.resource_type === 'handoff_ticket' ? '工单' : '售后单' }}</dt><dd><code>{{ action.receipt.resource_id }}</code></dd>
            </template>
          </template>
        </dl>
        <AgentDetails v-if="trace" :citations="[]" :trace="trace" :action="action ?? undefined" />
        <p v-else class="empty"><el-icon aria-hidden="true"><Files /></el-icon> 步骤记录只随当次回复返回，刷新页面后不再显示。</p>
      </el-tab-pane>
    </el-tabs>
  </el-drawer>
</template>

<style scoped>
.title h2 { margin: 0; color: var(--app-text); font-size: 18px; }
.title p { margin: 4px 0 0; color: var(--app-text-muted); font-size: 13px; }
.tabs :deep(.el-tabs__header) { margin-bottom: 18px; }
.block { display: flex; flex-direction: column; gap: 10px; margin-bottom: 22px; }
.block h3 { margin: 0; color: var(--app-text-body); font-size: 12px; font-weight: 600; letter-spacing: 1px; }
.verdict { display: flex; flex-direction: column; gap: 4px; padding: 14px; border: 1px solid transparent; border-radius: 12px; }
.verdict strong { font-size: 15px; }
.verdict p { margin: 0; color: var(--app-text-body); font-size: 13px; line-height: 1.7; }
.tone-amber { border-color: #f3dfb8; background: #fff8ec; }
.tone-amber strong { color: var(--tone-amber); }
.tone-green { border-color: #cfe8da; background: var(--tone-green-bg); }
.tone-green strong { color: var(--tone-green); }
.tone-red { border-color: #f4cdc9; background: var(--tone-red-bg); }
.tone-red strong { color: var(--tone-red); }
.code { margin-top: 6px; color: var(--app-text-muted); font-size: 12px; }
code { font-family: "Cascadia Code", Consolas, "SFMono-Regular", monospace; font-size: 12px; overflow-wrap: anywhere; }
.rows { display: flex; flex-direction: column; gap: 8px; margin: 0; padding: 0; list-style: none; }
.row { display: flex; align-items: flex-start; gap: 12px; padding: 12px 14px; border: 1px solid #efe9e3; border-radius: 10px; background: var(--app-surface); }
.row-icon { flex: none; width: 32px; height: 32px; border-radius: 8px; background: var(--app-bg); color: var(--app-text-body); }
.row-text { display: flex; flex: 1; flex-direction: column; gap: 3px; min-width: 0; font-size: 14px; overflow-wrap: anywhere; }
.row-text span { color: var(--app-text-muted); font-size: 12px; line-height: 1.6; }
.source { flex: none; padding: 2px 6px; border-radius: 4px; background: var(--tone-gray-bg); color: var(--tone-gray); font-size: 11px; font-weight: 600; }
.source.kb { background: var(--tone-blue-bg); color: var(--tone-blue); }
.empty { display: flex; align-items: center; gap: 6px; margin: 0; color: var(--app-text-muted); font-size: 13px; line-height: 1.7; }
.timeline { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
.timeline li { position: relative; display: grid; grid-template-columns: 18px 1fr; column-gap: 12px; padding-bottom: 18px; }
.timeline li:not(:last-child)::after { position: absolute; top: 20px; bottom: 2px; left: 8px; width: 2px; content: ''; background: #ece6e0; }
.tl-dot { width: 18px; height: 18px; margin-top: 1px; border-radius: 9px; background: var(--tone-green-dot); }
.tl-text { display: flex; flex-direction: column; gap: 2px; }
.tl-text strong { font-size: 14px; }
.tl-text time, .tl-text code { color: var(--app-text-muted); font-size: 12px; }
.facts { display: grid; grid-template-columns: 72px 1fr; gap: 8px 12px; margin: 0 0 16px; padding: 14px; border: 1px solid #efe9e3; border-radius: 10px; background: var(--app-surface-muted); font-size: 13px; }
.facts dt { color: var(--app-text-muted); }
.facts dd { margin: 0; min-width: 0; }
.arg { display: block; }
</style>
