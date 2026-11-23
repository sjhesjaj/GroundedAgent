<script setup lang="ts">
// One order: its lines, every package it shipped in, and the amount. A line
// offers 申请售后 (paid orders only) and 咨询; both only prepare a draft.
import { Van } from '@element-plus/icons-vue'
import { computed } from 'vue'

import StatusTag from '@/components/StatusTag.vue'
import type { Order, OrderItem, OrderPackage } from '@/types'
import { dateTime, money, shortTime } from '@/utils/format'
import type { LineBadge, Tone } from '@/utils/outcome'

import ProductThumb from './ProductThumb.vue'

const props = withDefaults(defineProps<{
  order: Order
  /** Badges keyed by order_item_id: an after-sales request of this session on that line. */
  badges?: Record<string, LineBadge>
  disabled?: boolean
}>(), { badges: () => ({}), disabled: false })

const emit = defineEmits<{ apply: [item: OrderItem], ask: [item: OrderItem] }>()

const ORDER_TONES: Record<string, Tone> = { 已签收: 'green', 已发货: 'blue', 待付款: 'peach', 已取消: 'gray' }
const PACKAGE_TONES: Record<string, Tone> = { 已签收: 'green', 运输中: 'blue' }

const paid = computed(() => Boolean(props.order.paid_at))
const orderTone = computed(() => ORDER_TONES[props.order.status] ?? 'gray')

function packageTime(pkg: OrderPackage) {
  return pkg.delivered_at ? `${shortTime(pkg.delivered_at)} 签收` : `最近更新 ${shortTime(pkg.last_event_at)}`
}
</script>

<template>
  <article class="order-card" :aria-label="`订单 ${order.order_id}`">
    <header class="head">
      <div class="meta">
        <strong>{{ order.order_id }}</strong>
        <span>{{ paid ? `付款时间 ${dateTime(order.paid_at)}` : '尚未付款' }}</span>
      </div>
      <StatusTag :tone="orderTone">{{ order.status }}</StatusTag>
    </header>

    <div v-for="item in order.items" :key="item.order_item_id" class="line"
      :class="{ 'line-active': badges[item.order_item_id] }">
      <ProductThumb :category="item.category" />
      <div class="info">
        <span class="name">{{ item.product_name }}</span>
        <span class="spec">{{ item.category }} · {{ item.sku }}</span>
        <span class="price">下单单价 {{ money(item.unit_price) }} × {{ item.quantity }}</span>
      </div>
      <div class="subtotal">{{ money(item.subtotal) }}<small>商品小计</small></div>
      <div class="actions">
        <StatusTag v-if="badges[item.order_item_id]" class="badge" :tone="badges[item.order_item_id].tone">
          {{ badges[item.order_item_id].text }}
        </StatusTag>
        <el-button v-if="paid" class="apply" type="primary" plain :disabled="disabled" @click="emit('apply', item)">
          申请售后
        </el-button>
        <el-button class="ask" :disabled="disabled" @click="emit('ask', item)">咨询</el-button>
        <span v-if="!paid" class="unpaid">订单未付款</span>
      </div>
    </div>

    <ul v-if="order.packages.length" class="packages" :aria-label="`${order.packages.length} 个包裹`">
      <li v-for="pkg in order.packages" :key="pkg.tracking_no" class="package">
        <el-icon class="van" aria-hidden="true"><Van /></el-icon>
        <span>{{ pkg.carrier }} {{ pkg.tracking_no }}</span>
        <StatusTag :tone="PACKAGE_TONES[pkg.status] ?? 'gray'">{{ pkg.status }}</StatusTag>
        <span class="when">{{ packageTime(pkg) }}</span>
      </li>
    </ul>

    <footer class="foot">订单金额<strong>{{ money(order.total_amount) }}</strong></footer>
  </article>
</template>

<style scoped>
.order-card {
  overflow: hidden;
  background: var(--app-surface);
  border: 1px solid var(--app-border);
  border-radius: 12px;
  transition: box-shadow var(--motion-base);
}
.order-card:hover { box-shadow: 0 8px 24px rgb(31 26 23 / 7%); }
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 20px;
  background: var(--app-surface-muted);
  border-bottom: 1px solid #efe9e3;
}
.meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 16px; color: var(--app-text-muted); font-size: 13px; }
.meta strong { color: var(--app-text); font-size: 14px; font-variant-numeric: tabular-nums; }
.line {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 16px 20px;
  border-bottom: 1px solid #f3eee9;
  transition: background-color var(--motion-fast);
}
.line:hover { background: #fdfbf9; }
.line-active { background: #fffaf6; }
.info { display: flex; flex: 1; flex-direction: column; gap: 4px; min-width: 0; }
.name { font-size: 15px; font-weight: 600; }
.spec { color: var(--app-text-muted); font-size: 12px; }
.price { color: var(--app-text-body); font-size: 13px; font-variant-numeric: tabular-nums; }
.subtotal { width: 112px; text-align: right; font-size: 15px; font-weight: 700; font-variant-numeric: tabular-nums; }
.subtotal small { display: block; color: var(--app-text-muted); font-size: 12px; font-weight: 400; }
.actions { display: flex; flex-direction: column; align-items: stretch; gap: 8px; width: 112px; }
.actions :deep(.el-button) { margin: 0; }
.badge { justify-content: center; }
.unpaid { color: var(--app-text-muted); font-size: 12px; text-align: center; }
.packages { display: flex; flex-direction: column; gap: 8px; margin: 0; padding: 12px 20px; list-style: none; }
.package { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; color: var(--app-text-body); font-size: 13px; }
.van { color: var(--app-text-muted); font-size: 18px; }
.when { color: var(--app-text-muted); font-variant-numeric: tabular-nums; }
.foot {
  display: flex;
  align-items: baseline;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid #f3eee9;
  color: var(--app-text-muted);
  font-size: 13px;
}
.foot strong { color: var(--el-color-primary-dark-2); font-size: 18px; font-variant-numeric: tabular-nums; }

/* A narrow column (the conversation view, or a phone): actions go under the line. */
@container orders (max-width: 560px) {
  .line { flex-wrap: wrap; gap: 12px; padding: 14px 16px; }
  .subtotal { width: auto; }
  .actions { flex-direction: row; flex-wrap: wrap; align-items: center; justify-content: flex-end; width: 100%; }
  .actions :deep(.el-button) { flex: 0 0 auto; }
  .head, .packages, .foot { padding-left: 16px; padding-right: 16px; }
}
</style>
