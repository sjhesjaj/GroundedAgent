<script setup lang="ts">
// The right column of the approval desk. 审批详情: the action, the order line it
// names (read-only, GET …/order-summary), the Guard's verdict, the submitted
// arguments, and 拒绝 / 批准 while it waits. 处理记录: its audit trail.
// The order lookup failing never hides the action or blocks the decision.
import { Refresh } from '@element-plus/icons-vue'
import { computed, ref } from 'vue'

import ProductThumb from '@/components/customer/ProductThumb.vue'
import StatusTag from '@/components/StatusTag.vue'
import type { ActionView, AuditEvent, Decision } from '@/types'
import { actionTag, actionTitle, deskResult, type SummaryState } from '@/utils/desk'
import { REASON_LABELS, type ReasonCode } from '@/utils/draft'
import { clockTime, dateTime, money, shortTime } from '@/utils/format'
import { actionEvents, eventTitle, guardVerdict } from '@/utils/outcome'

const props = withDefaults(defineProps<{
  actions: ActionView[]
  action: ActionView | null
  audit?: AuditEvent[]
  summary?: SummaryState
  /** Another request is running: the buttons wait. */
  busy?: boolean
  deciding?: boolean
}>(), { audit: () => [], summary: () => ({ state: 'idle' }), busy: false, deciding: false })

const emit = defineEmits<{ focus: [pendingActionId: string], decide: [decision: Decision], reload: [] }>()

const tab = ref<'detail' | 'log'>('detail')

const tag = computed(() => props.action ? actionTag(props.action) : null)
const verdict = computed(() => guardVerdict(props.action))
const result = computed(() => props.action ? deskResult(props.action) : null)
const waiting = computed(() => props.action?.status === 'WAITING_APPROVAL' && Boolean(props.action.pending_action_id))
const events = computed(() => props.action ? actionEvents(props.audit, props.action) : [])
const order = computed(() => 'data' in props.summary ? props.summary.data.order : null)
const item = computed(() => 'data' in props.summary ? props.summary.data.item : null)

const ARGUMENT_WORDS: Record<string, string> = {
  order_id: '订单', order_item_id: '商品行', reason_code: '原因', target_sku: '换成规格', note: '补充说明',
}
const argumentsList = computed(() => Object.entries(props.action?.arguments ?? {}).map(([key, value]) => ({
  key,
  label: ARGUMENT_WORDS[key] ?? key,
  value: String(value),
  words: key === 'reason_code' ? REASON_LABELS[value as ReasonCode] : '',
})))

const packageLine = (pkg: { carrier: string, tracking_no: string, status: string, delivered_at: string | null, last_event_at: string }) =>
  `${pkg.carrier} ${pkg.tracking_no} · ${pkg.status} ${pkg.delivered_at ? `${shortTime(pkg.delivered_at)} 签收` : `最近更新 ${shortTime(pkg.last_event_at)}`}`
</script>

<template>
  <aside class="review" aria-label="审批详情">
    <div class="tabs" role="tablist">
      <button type="button" role="tab" class="tab" :class="{ on: tab === 'detail' }" :aria-selected="tab === 'detail'" @click="tab = 'detail'">审批详情</button>
      <button type="button" role="tab" class="tab" :class="{ on: tab === 'log' }" :aria-selected="tab === 'log'" @click="tab = 'log'">处理记录</button>
    </div>

    <div v-if="!action" class="placeholder"><p>这条会话没有售后申请。</p></div>

    <template v-else-if="tab === 'detail'">
      <div class="body">
        <div v-if="actions.length > 1" class="switch" role="group" aria-label="本会话的申请">
          <button v-for="(entry, index) in actions" :key="entry.pending_action_id ?? `denied-${index}`" type="button" class="chip"
            :class="{ on: entry.pending_action_id === action.pending_action_id }" :disabled="!entry.pending_action_id"
            @click="entry.pending_action_id && emit('focus', entry.pending_action_id)">
            {{ actionTitle(entry) }}<StatusTag :tone="actionTag(entry).tone">{{ actionTag(entry).text }}</StatusTag>
          </button>
        </div>

        <div class="act" :data-status="action.status">
          <div class="act-row"><strong>{{ actionTitle(action) }}</strong>
            <StatusTag v-if="tag" :tone="tag.tone" :pulse="waiting">{{ tag.text }}</StatusTag></div>
          <span v-if="action.pending_action_id" class="mono">{{ action.pending_action_id }}</span>
        </div>

        <section class="section" aria-label="订单核对">
          <div class="section-head"><span class="section-title">订单核对</span><span class="note">来自订单记录，只读</span></div>
          <el-skeleton v-if="summary.state === 'loading'" class="goods-skeleton" animated :rows="3" />
          <template v-else>
            <div v-if="item || order" class="goods">
              <div v-if="item" class="goods-top">
                <ProductThumb :category="item.category" :size="52" />
                <div><strong>{{ item.product_name }}</strong><span>{{ item.category }} · {{ item.sku }}</span><span>商品行 {{ item.order_item_id }}</span></div>
              </div>
              <div v-if="item" class="nums">
                <div class="num"><span>数量</span><strong>{{ item.quantity }}</strong></div>
                <div class="num"><span>下单单价</span><strong>{{ money(item.unit_price) }}</strong></div>
                <div class="num em"><span>商品小计</span><strong>{{ money(item.subtotal) }}</strong></div>
              </div>
              <dl v-if="order" class="kv">
                <dt>订单</dt><dd>{{ order.order_id }} · {{ order.status }}</dd>
                <dt>付款时间</dt><dd>{{ order.paid_at ? dateTime(order.paid_at) : '未付款' }}</dd>
                <dt>订单金额</dt><dd>{{ money(order.total_amount) }}</dd>
                <dt>包裹</dt>
                <dd><span v-for="pkg in order.packages" :key="pkg.tracking_no" class="package">{{ packageLine(pkg) }}</span>
                  <span v-if="!order.packages.length">暂无包裹</span></dd>
              </dl>
            </div>
            <p v-if="summary.state === 'idle'" class="placeholder-text">这笔申请没有进入审批，没有可核对的订单记录。</p>
            <div v-else-if="summary.state === 'missing'" class="lookup" role="status" data-lookup="missing">
              <strong>订单记录里读不到这笔申请的{{ order ? '商品' : '订单' }}</strong>
              <p>对话和申请信息不受影响，可以先核对下方的申请参数；批准后服务端仍会重新核对订单和售后状态。</p>
              <el-button size="small" :icon="Refresh" :disabled="busy" @click="emit('reload')">重新读取</el-button>
            </div>
            <div v-else-if="summary.state === 'recovering'" class="lookup" role="status" data-lookup="recovering">
              <strong>会话正在恢复，订单信息暂时读不到</strong>
              <p>对话和申请信息不受影响。请稍后点「重新读取」。</p>
              <el-button size="small" :icon="Refresh" :disabled="busy" @click="emit('reload')">重新读取</el-button>
            </div>
            <div v-else-if="summary.state === 'error'" class="lookup bad" role="status" data-lookup="error">
              <strong>订单信息读取失败</strong>
              <p>{{ summary.message }} 对话和申请信息不受影响。</p>
              <el-button size="small" :icon="Refresh" :disabled="busy" @click="emit('reload')">重新读取</el-button>
            </div>
          </template>
        </section>

        <section v-if="verdict" class="section" aria-label="规则校验">
          <span class="section-title">规则校验</span>
          <div class="verdict" :class="`tone-${verdict.tone}`">
            <strong>{{ verdict.title }}</strong>
            <p>{{ verdict.text }}</p>
            <span v-if="action.guard" class="mono">{{ action.guard.decision }}<template v-if="action.guard.reason_code"> · {{ action.guard.reason_code }}</template></span>
          </div>
        </section>

        <details class="args" :open="summary.state === 'missing' || summary.state === 'recovering' || summary.state === 'error'">
          <summary>申请参数</summary>
          <dl>
            <template v-for="entry in argumentsList" :key="entry.key">
              <dt>{{ entry.label }}</dt>
              <dd><code>{{ entry.value }}</code><template v-if="entry.words"> · {{ entry.words }}</template></dd>
            </template>
          </dl>
        </details>
      </div>

      <footer class="foot">
        <template v-if="waiting">
          <p>批准后，服务端会重新核对订单和售后状态，一致才会执行。审批结论以服务端返回的结果为准。</p>
          <div class="buttons">
            <el-button class="reject" size="large" :disabled="busy" @click="emit('decide', 'REJECT')">拒绝</el-button>
            <el-button class="approve" type="primary" size="large" :disabled="busy" :loading="deciding" @click="emit('decide', 'APPROVE')">批准</el-button>
          </div>
        </template>
        <div v-else-if="result" class="result" :class="`tone-${result.tone}`" data-test="result">
          <strong>{{ result.title }}</strong>
          <span>{{ result.text }}</span>
          <span v-if="result.receipt" class="receipt">{{ result.receipt }}</span>
        </div>
      </footer>
    </template>

    <div v-else class="body">
      <span class="section-title">处理记录</span>
      <ol v-if="events.length" class="timeline">
        <li v-for="event in events" :key="event.event_seq">
          <span class="dot" aria-hidden="true"></span>
          <div class="event">
            <strong>{{ eventTitle(event) }}</strong>
            <span><time :datetime="event.at">{{ clockTime(event.at) }}</time><template v-if="event.approver_ref"> · 操作员 {{ event.approver_ref }}</template></span>
            <span class="mono">{{ event.event_name }}<template v-if="event.code"> · {{ event.code }}</template></span>
          </div>
        </li>
        <li v-if="waiting" class="pending">
          <span class="dot" aria-hidden="true"></span>
          <div class="event"><strong>等待操作员审批</strong><span>审批后会记录人工决定、重新核对和执行结果。</span></div>
        </li>
      </ol>
      <p v-else class="placeholder-text">还没有处理记录。</p>
    </div>
  </aside>
</template>

<style scoped>
.review { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--app-surface); border-left: 1px solid var(--app-border); }
.tabs { display: flex; gap: 20px; padding: 0 20px; border-bottom: 1px solid #efe9e3; flex: none; }
.tab { position: relative; height: 48px; padding: 0; border: 0; background: transparent; color: var(--app-text-body); font: inherit; font-size: 14px; cursor: pointer; }
.tab.on { color: var(--el-color-primary-dark-2); font-weight: 600; }
.tab.on::after { position: absolute; right: 0; bottom: -1px; left: 0; height: 2px; border-radius: 1px; content: ''; background: var(--el-color-primary); }
.tab:focus-visible, .chip:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.body { display: flex; flex: 1; flex-direction: column; gap: 18px; min-height: 0; overflow-y: auto; padding: 18px 20px; }
.placeholder { display: grid; flex: 1; place-items: center; padding: 40px; color: var(--app-text-muted); font-size: 14px; text-align: center; }
.placeholder-text { margin: 0; color: var(--app-text-muted); font-size: 13px; }
.switch { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { display: inline-flex; align-items: center; gap: 8px; padding: 6px 8px 6px 12px; border: 1px solid var(--app-border); border-radius: 18px; background: var(--app-surface); color: var(--app-text); font: inherit; font-size: 13px; cursor: pointer; }
.chip.on { border-color: #efcbbc; background: #fdf6f2; }
.chip:disabled { cursor: default; opacity: 0.75; }
.act { display: flex; flex-direction: column; gap: 6px; animation: app-rise var(--motion-base) ease-out both; }
.act-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.act-row strong { font-size: 17px; }
.mono { color: var(--app-text-muted); font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; }
.section { display: flex; flex-direction: column; gap: 10px; }
.section-head { display: flex; align-items: baseline; justify-content: space-between; }
.section-title { color: var(--app-text-body); font-size: 12px; font-weight: 600; letter-spacing: 1px; }
.note { color: var(--app-text-muted); font-size: 12px; }
.goods { overflow: hidden; border: 1px solid var(--app-border); border-radius: 12px; }
.goods-top { display: flex; align-items: center; gap: 12px; padding: 12px 14px; }
.goods-top div { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.goods-top strong { font-size: 15px; }
.goods-top span { color: var(--app-text-muted); font-size: 12px; }
.nums { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border-top: 1px solid #f3eee9; }
.num { display: flex; flex-direction: column; gap: 2px; padding: 10px 14px; }
.num:not(:last-child) { border-right: 1px solid #f3eee9; }
.num span { color: var(--app-text-muted); font-size: 12px; }
.num strong { font-size: 15px; font-variant-numeric: tabular-nums; }
.num.em strong { color: var(--el-color-primary-dark-2); }
.kv { display: grid; grid-template-columns: 72px 1fr; gap: 8px 12px; margin: 0; padding: 12px 14px; background: var(--app-surface-muted); border-top: 1px solid #f3eee9; font-size: 13px; }
.goods > .kv:first-child { border-top: 0; }
.kv dt { color: var(--app-text-muted); }
.kv dd { display: flex; flex-direction: column; gap: 2px; margin: 0; }
.lookup { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; padding: 12px 14px; border: 1px solid #f3dfb8; border-radius: 12px; background: #fff8ec; animation: app-rise var(--motion-base) ease-out both; }
.lookup.bad { border-color: #f1c7c2; background: var(--tone-red-bg); }
.lookup strong { font-size: 14px; }
.lookup p { margin: 0; color: var(--app-text-body); font-size: 13px; line-height: 1.6; }
.verdict { display: flex; flex-direction: column; gap: 4px; padding: 12px 14px; border: 1px solid; border-radius: 12px; }
.verdict strong { font-size: 14px; }
.verdict p { margin: 0 0 4px; color: var(--app-text-body); font-size: 13px; line-height: 1.6; }
.verdict.tone-amber { border-color: #f3dfb8; background: #fff8ec; }
.verdict.tone-green { border-color: #c6e6d3; background: var(--tone-green-bg); }
.verdict.tone-red { border-color: #f1c7c2; background: var(--tone-red-bg); }
.args { padding: 0 14px; border: 1px solid var(--app-border); border-radius: 10px; }
.args summary { display: flex; align-items: center; height: 42px; color: var(--app-text); font-size: 13px; font-weight: 600; cursor: pointer; }
.args dl { display: grid; grid-template-columns: 72px 1fr; gap: 6px 12px; margin: 0 0 12px; font-size: 13px; }
.args dt { color: var(--app-text-muted); }
.args dd { margin: 0; overflow-wrap: anywhere; }
.args code { font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; }
.foot { display: flex; flex-direction: column; gap: 10px; padding: 14px 20px 16px; background: var(--app-surface); border-top: 1px solid #efe9e3; flex: none; }
.foot p { margin: 0; color: var(--app-text-muted); font-size: 12px; line-height: 1.6; }
.buttons { display: flex; gap: 10px; }
.buttons .el-button { flex: 1; margin: 0; }
.reject { --el-button-text-color: var(--tone-red); --el-button-border-color: #e3b3ae; --el-button-hover-text-color: var(--tone-red); --el-button-hover-bg-color: var(--tone-red-bg); --el-button-hover-border-color: #d99a93; }
.result { display: flex; flex-direction: column; gap: 2px; padding: 12px 14px; border-radius: 10px; font-size: 13px; line-height: 1.6; animation: app-rise var(--motion-base) ease-out both; overflow-wrap: anywhere; }
.result strong { font-size: 14px; }
.result .receipt { font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; opacity: 0.85; }
.result.tone-green { background: var(--tone-green-bg); color: var(--tone-green); }
.result.tone-red { background: var(--tone-red-bg); color: var(--tone-red); }
.result.tone-gray { background: var(--tone-gray-bg); color: var(--tone-gray); }
.timeline { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
.timeline li { position: relative; display: grid; grid-template-columns: 18px 1fr; column-gap: 12px; padding-bottom: 16px; animation: app-rise var(--motion-base) ease-out both; }
.timeline li:not(:last-child)::after { position: absolute; top: 20px; bottom: 2px; left: 8px; width: 2px; content: ''; background: #ece6e0; }
.dot { width: 18px; height: 18px; margin-top: 1px; border-radius: 9px; background: var(--tone-green-dot); }
.pending .dot { border: 2px dashed var(--tone-amber-dot); background: var(--app-surface); }
.event { display: flex; flex-direction: column; gap: 2px; }
.event strong { font-size: 14px; }
.event span { color: var(--app-text-muted); font-size: 12px; }
</style>
