<script setup lang="ts">
// 我的订单: status filters, a search over order numbers and product names, and
// the order cards. Loading shows card-shaped skeletons; a failed load keeps
// the rest of the page usable and offers a retry.
import { Search } from '@element-plus/icons-vue'
import { computed, ref } from 'vue'

import type { Order, OrderItem } from '@/types'
import type { LineBadge } from '@/utils/outcome'

import OrderCard from './OrderCard.vue'

const props = withDefaults(defineProps<{
  orders: Order[]
  loading?: boolean
  error?: string
  /** order_id -> order_item_id -> badge */
  badges?: Record<string, Record<string, LineBadge>>
  disabled?: boolean
  subtitle?: string
}>(), { loading: false, error: '', badges: () => ({}), disabled: false, subtitle: '' })

const emit = defineEmits<{ apply: [order: Order, item: OrderItem], ask: [order: Order, item: OrderItem], retry: [] }>()

const STATUS_ORDER = ['待付款', '已发货', '已签收']

const filter = ref('all')
const query = ref('')

const filters = computed(() => {
  const counts = new Map<string, number>()
  for (const order of props.orders) counts.set(order.status, (counts.get(order.status) ?? 0) + 1)
  const statuses = [...counts.keys()].sort((a, b) => {
    const rank = (status: string) => (STATUS_ORDER.indexOf(status) + 1) || 99
    return rank(a) - rank(b)
  })
  return [{ key: 'all', label: '全部', count: props.orders.length },
    ...statuses.map((status) => ({ key: status, label: status, count: counts.get(status) ?? 0 }))]
})

const shown = computed(() => {
  const words = query.value.trim().toLowerCase()
  return props.orders.filter((order) => (filter.value === 'all' || order.status === filter.value)
    && (!words || order.order_id.toLowerCase().includes(words)
      || order.items.some((item) => item.product_name.toLowerCase().includes(words))))
})
</script>

<template>
  <section class="order-list" aria-labelledby="orders-title">
    <div class="heading">
      <h1 id="orders-title">我的订单</h1>
      <p>{{ subtitle || '找到要处理的商品，点「申请售后」；不确定能不能办，可以先点「咨询」。' }}</p>
    </div>

    <div class="tools">
      <div class="filters" role="group" aria-label="按订单状态筛选">
        <button v-for="item in filters" :key="item.key" type="button" class="chip" :class="{ on: filter === item.key }"
          :aria-pressed="filter === item.key" @click="filter = item.key">
          {{ item.label }}<b>{{ item.count }}</b>
        </button>
      </div>
      <el-input v-model="query" class="search" :prefix-icon="Search" clearable placeholder="搜索商品名或订单号"
        aria-label="搜索商品名或订单号" />
    </div>

    <el-alert v-if="error" class="error" type="error" :title="error" :closable="false" show-icon>
      <el-button size="small" @click="emit('retry')">重新加载</el-button>
    </el-alert>

    <div v-if="loading" class="cards" aria-busy="true" aria-label="正在加载订单">
      <div v-for="index in 3" :key="index" class="skeleton-card">
        <el-skeleton animated :rows="0">
          <template #template>
            <el-skeleton-item variant="text" style="width: 40%" />
            <div class="skeleton-line">
              <el-skeleton-item variant="rect" style="width: 64px; height: 64px; border-radius: 10px" />
              <div class="skeleton-text">
                <el-skeleton-item variant="text" style="width: 60%" />
                <el-skeleton-item variant="text" style="width: 35%" />
              </div>
            </div>
          </template>
        </el-skeleton>
      </div>
    </div>

    <el-empty v-else-if="!error && !shown.length" :image-size="96"
      :description="orders.length ? '没有符合条件的订单' : '还没有订单'" />

    <TransitionGroup v-else tag="div" name="card" class="cards">
      <OrderCard v-for="(order, index) in shown" :key="order.order_id" :order="order"
        :style="{ '--delay': `${index * 50}ms` }" :badges="badges[order.order_id]" :disabled="disabled"
        @apply="(item) => emit('apply', order, item)" @ask="(item) => emit('ask', order, item)" />
    </TransitionGroup>
  </section>
</template>

<style scoped>
.order-list { display: flex; flex-direction: column; gap: 16px; min-width: 0; container: orders / inline-size; }
.heading h1 { margin: 0; font-size: 22px; font-weight: 700; }
.heading p { margin: 4px 0 0; color: var(--app-text-muted); font-size: 13px; line-height: 1.6; }
.tools { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 14px;
  border: 1px solid var(--app-border);
  border-radius: 17px;
  background: var(--app-surface);
  color: #3a332e;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: background-color var(--motion-fast), border-color var(--motion-fast);
}
.chip:hover { border-color: #cfc6be; }
.chip:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.chip b { color: var(--app-text-muted); font-weight: 600; font-variant-numeric: tabular-nums; }
.chip.on { border-color: var(--el-color-primary-light-5); background: var(--tone-peach-bg); color: var(--tone-peach); font-weight: 600; }
.chip.on b { color: var(--tone-peach); }
.search { width: 260px; margin-left: auto; }
.error :deep(.el-alert__description) { margin-top: 6px; }
.cards { display: flex; flex-direction: column; gap: 16px; }
.cards > * { animation: app-rise 320ms ease-out both; animation-delay: var(--delay, 0ms); }
.skeleton-card { padding: 16px 20px; background: var(--app-surface); border: 1px solid var(--app-border); border-radius: 12px; }
.skeleton-line { display: flex; gap: 16px; align-items: center; margin-top: 14px; }
.skeleton-text { display: flex; flex: 1; flex-direction: column; gap: 10px; }
.card-leave-active { transition: opacity var(--motion-fast); }
.card-leave-to { opacity: 0; }

@container orders (max-width: 560px) {
  .search { width: 100%; margin-left: 0; }
}
</style>
