<script setup lang="ts">
// 申请进度: one after-sales action in the customer's words (utils/outcome.ts),
// the product it is about, the steps so far and what to do next. Customers
// never decide here; the only server call it leads to is the session re-read.
import { CircleCheckFilled, CircleCloseFilled, Document, Refresh } from '@element-plus/icons-vue'
import { computed } from 'vue'

import StatusTag from '@/components/StatusTag.vue'
import type { ActionView, OrderItem } from '@/types'
import { money } from '@/utils/format'
import { describeAction, type NextKind } from '@/utils/outcome'

import ProductThumb from './ProductThumb.vue'

const props = withDefaults(defineProps<{
  action: ActionView
  /** The product line the action names, when it is among the customer's orders. */
  item?: OrderItem | null
  disabled?: boolean
}>(), { item: null, disabled: false })

const emit = defineEmits<{ next: [kind: NextKind], evidence: [] }>()

const outcome = computed(() => describeAction(props.action))
const orderId = computed(() => props.action.arguments?.order_id ?? '')
</script>

<template>
  <article class="application" :data-status="action.status" :aria-label="`${outcome.title} · ${outcome.tag}`">
    <header class="head">
      <strong>{{ outcome.title }}</strong>
      <StatusTag :tone="outcome.tone" :pulse="outcome.pulse">{{ outcome.tag }}</StatusTag>
    </header>

    <div v-if="item || orderId" class="product">
      <ProductThumb :category="item?.category" :size="36" />
      <div class="product-text">
        <strong>{{ item?.product_name ?? action.arguments?.order_item_id ?? '订单商品' }}</strong>
        <span>订单 {{ orderId }}<template v-if="item"> · ×{{ item.quantity }} · 商品小计 {{ money(item.subtotal) }}</template></span>
      </div>
    </div>

    <ol class="steps">
      <li v-for="(step, index) in outcome.steps" :key="step.title" class="step" :class="`step-${step.state}`">
        <span class="dot" aria-hidden="true">
          <el-icon v-if="step.state === 'done'"><CircleCheckFilled /></el-icon>
          <el-icon v-else-if="step.state === 'bad'"><CircleCloseFilled /></el-icon>
          <template v-else>{{ index + 1 }}</template>
        </span>
        <div class="step-text"><strong>{{ step.title }}</strong><span>{{ step.text }}</span></div>
      </li>
    </ol>

    <p class="summary">{{ outcome.summary }}</p>

    <footer class="foot">
      <p v-if="outcome.hint" class="hint">{{ outcome.hint }}</p>
      <div class="buttons">
        <el-button v-for="step in outcome.next" :key="step.kind" :type="step.kind === 'refresh' ? 'primary' : 'default'"
          :plain="step.kind === 'refresh'" :icon="step.kind === 'refresh' ? Refresh : undefined" :disabled="disabled"
          @click="emit('next', step.kind)">{{ step.label }}</el-button>
        <el-button class="evidence" text type="primary" :icon="Document" @click="emit('evidence')">处理依据</el-button>
      </div>
    </footer>
  </article>
</template>

<style scoped>
.application {
  overflow: hidden;
  background: var(--app-surface);
  border: 1px solid #ece6e0;
  border-radius: 14px;
  animation: app-rise var(--motion-slow) ease-out both;
}
.head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 14px; border-bottom: 1px solid #f3eee9; }
.head strong { font-size: 15px; }
.product { display: flex; align-items: center; gap: 10px; padding: 10px 14px; background: var(--app-surface-muted); }
.product-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; color: var(--app-text-muted); font-size: 12px; }
.product-text strong { color: var(--app-text); font-size: 13px; }
.steps { display: flex; flex-direction: column; margin: 0; padding: 14px 14px 2px; list-style: none; }
.step { position: relative; display: grid; grid-template-columns: 22px 1fr; column-gap: 10px; padding-bottom: 14px; }
.step:not(:last-child)::after {
  position: absolute;
  top: 24px;
  bottom: 2px;
  left: 10px;
  width: 2px;
  content: '';
  background: #ece6e0;
}
.step-done:not(:last-child)::after { background: var(--tone-green-dot); }
.dot {
  position: relative;
  z-index: 1;
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 11px;
  background: var(--tone-gray-bg);
  color: var(--app-text-muted);
  font-size: 11px;
  font-weight: 700;
}
.step-done .dot { background: transparent; color: var(--tone-green-dot); font-size: 22px; }
.step-bad .dot { background: transparent; color: var(--tone-red-dot); font-size: 22px; }
.step-now .dot { background: var(--tone-amber-dot); color: #fff; animation: ring 1.6s ease-out infinite; }
.step-text { display: flex; flex-direction: column; gap: 2px; padding-top: 1px; }
.step-text strong { font-size: 13px; }
.step-text span { color: var(--app-text-muted); font-size: 12px; line-height: 1.6; }
.step-off .step-text strong { color: var(--app-text-muted); font-weight: 500; }
.summary { margin: 0; padding: 0 14px 12px; color: var(--app-text-body); font-size: 13px; line-height: 1.7; }
.foot { display: flex; flex-direction: column; gap: 8px; padding: 10px 14px 12px; border-top: 1px solid #f3eee9; }
.hint { margin: 0; color: var(--app-text-muted); font-size: 12px; line-height: 1.6; }
.buttons { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.buttons :deep(.el-button + .el-button) { margin-left: 0; }
/* Alone on its row (nothing to do next), the link lines up with the hint. */
.evidence:first-child { padding-left: 0; }

@keyframes ring {
  0% { box-shadow: 0 0 0 0 rgb(194 124 14 / 45%); }
  100% { box-shadow: 0 0 0 8px rgb(194 124 14 / 0%); }
}
</style>
