<script setup lang="ts">
// 申请售后: pick the service and the reason for one product line; the result is
// a sentence for the composer (utils/draft.ts). Nothing is sent from here.
import { computed, ref, watch } from 'vue'

import type { Order, OrderItem } from '@/types'
import { composeDraft, REASON_LABELS, REASONS_FOR, SERVICE_OPTIONS, type ReasonCode, type ServiceKind } from '@/utils/draft'
import { money } from '@/utils/format'

import ProductThumb from './ProductThumb.vue'

const props = defineProps<{ order: Order | null, item: OrderItem | null }>()
const open = defineModel<boolean>({ required: true })
const emit = defineEmits<{ use: [text: string] }>()

const kind = ref<ServiceKind>('return')
const reason = ref<ReasonCode | null>('no_longer_wanted')
const note = ref('')

// A new line starts from the defaults.
watch(() => [props.order?.order_id, props.item?.order_item_id, open.value], () => {
  if (!open.value) return
  kind.value = 'return'
  reason.value = 'no_longer_wanted'
  note.value = ''
})

const reasons = computed(() => REASONS_FOR[kind.value])
watch(kind, () => {
  if (!reason.value || !reasons.value.includes(reason.value)) reason.value = reasons.value[0] ?? null
})

const noteLabel = computed(() => (kind.value === 'exchange' ? '想换成的规格（选填）' : '补充说明（选填）'))
const notePlaceholder = computed(() => (kind.value === 'exchange'
  ? '例如：换成 XL 码。不填的话，智能客服会再问你'
  : '例如：包装还没拆'))

const draft = computed(() => (props.order && props.item
  ? composeDraft(kind.value, { order_id: props.order.order_id, product_name: props.item.product_name }, reason.value, note.value)
  : ''))

function use() {
  if (!draft.value) return
  emit('use', draft.value)
  open.value = false
}
</script>

<template>
  <el-drawer v-model="open" class="apply-sheet" direction="rtl" size="min(520px, 100%)" title="申请售后"
    :append-to-body="true" destroy-on-close>
    <div v-if="order && item" class="body">
      <div class="product">
        <ProductThumb :category="item.category" :size="56" />
        <div class="product-text">
          <strong>{{ item.product_name }}</strong>
          <span>订单 {{ order.order_id }} · {{ item.category }}</span>
          <span>下单单价 {{ money(item.unit_price) }} × {{ item.quantity }}</span>
        </div>
      </div>

      <section class="block">
        <h3 id="kind-title">服务类型</h3>
        <div class="kinds" role="radiogroup" aria-labelledby="kind-title">
          <button v-for="option in SERVICE_OPTIONS" :key="option.kind" type="button" role="radio" class="kind"
            :class="{ on: kind === option.kind }" :aria-checked="kind === option.kind" @click="kind = option.kind">
            <strong>{{ option.label }}</strong><span>{{ option.description }}</span>
          </button>
        </div>
      </section>

      <section v-if="reasons.length" class="block">
        <h3 id="reason-title">申请原因</h3>
        <div class="reasons" role="radiogroup" aria-labelledby="reason-title">
          <button v-for="code in reasons" :key="code" type="button" role="radio" class="reason"
            :class="{ on: reason === code }" :aria-checked="reason === code" @click="reason = code">
            {{ REASON_LABELS[code] }}
          </button>
        </div>
      </section>

      <section class="block">
        <label class="label" for="apply-note">{{ noteLabel }}</label>
        <el-input id="apply-note" v-model="note" type="textarea" :rows="3" maxlength="300" show-word-limit
          :placeholder="notePlaceholder" />
      </section>

      <section class="block">
        <h3>将带入对话的内容</h3>
        <p class="preview" data-test="draft-preview">{{ draft }}</p>
      </section>
    </div>
    <template #footer>
      <div class="foot">
        <p>智能客服会先核对订单和规则；需要审核的申请会转给售后专员。发送前你还可以修改。</p>
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" class="use-draft" :disabled="!draft" @click="use">带入对话</el-button>
      </div>
    </template>
  </el-drawer>
</template>

<style scoped>
.body { display: flex; flex-direction: column; gap: 22px; }
.product { display: flex; align-items: center; gap: 14px; padding: 14px; border: 1px solid #efe9e3; border-radius: 12px; background: var(--app-surface-muted); }
.product-text { display: flex; flex-direction: column; gap: 3px; min-width: 0; font-size: 12px; color: var(--app-text-muted); }
.product-text strong { color: var(--app-text); font-size: 15px; }
.block { display: flex; flex-direction: column; gap: 10px; }
.block h3, .label { margin: 0; font-size: 14px; font-weight: 600; color: var(--app-text); }
.kinds { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.kind {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  min-height: 44px;
  padding: 12px;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: var(--app-surface);
  color: var(--app-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--motion-fast), background-color var(--motion-fast), box-shadow var(--motion-fast);
}
.kind strong { font-size: 14px; }
.kind span { color: var(--app-text-muted); font-size: 12px; line-height: 1.5; }
.kind.on { border-color: var(--el-color-primary); background: #fdf6f2; box-shadow: inset 0 0 0 1px var(--el-color-primary); }
.reasons { display: flex; flex-wrap: wrap; gap: 8px; }
.reason {
  height: 36px;
  padding: 0 14px;
  border: 1px solid var(--app-border);
  border-radius: 18px;
  background: var(--app-surface);
  color: #3a332e;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: border-color var(--motion-fast), background-color var(--motion-fast);
}
.reason.on { border-color: var(--el-color-primary); background: var(--tone-peach-bg); color: var(--tone-peach); font-weight: 600; }
.kind:focus-visible, .reason:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.preview { margin: 0; padding: 14px; border-radius: 10px; background: var(--app-bg); font-size: 14px; line-height: 1.7; }
.foot { display: flex; align-items: center; gap: 12px; }
.foot p { flex: 1; margin: 0; color: var(--app-text-muted); font-size: 12px; line-height: 1.6; text-align: left; }
.foot :deep(.el-button + .el-button) { margin-left: 0; }

@media (max-width: 560px) {
  .kinds { grid-template-columns: minmax(0, 1fr); }
  .foot { flex-wrap: wrap; }
  .foot p { flex-basis: 100%; }
}
</style>
