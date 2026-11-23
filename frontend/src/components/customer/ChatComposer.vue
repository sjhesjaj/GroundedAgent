<script setup lang="ts">
// The message box: an optional product card the message is about, the text
// (Enter sends, Shift+Enter breaks the line, at most 2000 characters), and
// 选订单 to attach one of the customer's products.
import { Close, Promotion, Tickets } from '@element-plus/icons-vue'
import { computed, ref } from 'vue'

import type { Order } from '@/types'
import type { Attachment } from '@/utils/draft'
import { money } from '@/utils/format'

import ProductThumb from './ProductThumb.vue'

const props = withDefaults(defineProps<{
  attachment?: Attachment | null
  orders?: Order[]
  /** No session, or one that cannot take a message right now. */
  disabled?: boolean
  sending?: boolean
  canSend?: boolean
  placeholder?: string
}>(), { attachment: null, orders: () => [], disabled: false, sending: false, canSend: false,
  placeholder: '描述你的售后问题，Enter 发送，Shift + Enter 换行' })

const text = defineModel<string>({ required: true })
const emit = defineEmits<{ send: [], detach: [], attach: [attachment: Attachment] }>()

const box = ref<HTMLTextAreaElement | null>(null)
const pickerOpen = ref(false)

const choices = computed(() => props.orders.filter((order) => order.paid_at)
  .flatMap((order) => order.items.map((item) => ({ order_id: order.order_id, item }))))

function keydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault()
    emit('send')
  }
}

function pick(choice: Attachment) {
  pickerOpen.value = false
  emit('attach', choice)
}

defineExpose({ focus: () => box.value?.focus() })
</script>

<template>
  <form class="composer" @submit.prevent="emit('send')">
    <Transition name="attach">
      <div v-if="attachment" class="attachment">
        <ProductThumb :category="attachment.item.category" :size="36" />
        <span class="attachment-text">
          {{ attachment.item.product_name }}
          <small>订单 {{ attachment.order_id }} · {{ money(attachment.item.unit_price) }} × {{ attachment.item.quantity }}</small>
        </span>
        <el-button text circle :icon="Close" aria-label="移除商品卡片" @click="emit('detach')" />
      </div>
    </Transition>
    <label class="sr-only" for="message">售后问题</label>
    <textarea id="message" ref="box" v-model="text" class="text" rows="2" maxlength="2000" :disabled="disabled"
      :placeholder="sending ? '正在回复，请稍候…' : placeholder" @keydown="keydown"></textarea>
    <div class="row">
      <el-popover v-model:visible="pickerOpen" trigger="click" placement="top-start" :width="320" :teleported="true">
        <template #reference>
          <el-button :icon="Tickets" :disabled="disabled || !choices.length">选订单</el-button>
        </template>
        <div class="picker" role="list" aria-label="选择要咨询的商品">
          <p class="picker-title">选择要咨询的商品</p>
          <button v-for="choice in choices" :key="`${choice.order_id}/${choice.item.order_item_id}`" type="button"
            role="listitem" class="choice" @click="pick(choice)">
            <ProductThumb :category="choice.item.category" :size="36" />
            <span class="attachment-text">{{ choice.item.product_name }}
              <small>订单 {{ choice.order_id }} · {{ money(choice.item.unit_price) }} × {{ choice.item.quantity }}</small></span>
          </button>
        </div>
      </el-popover>
      <span class="count">{{ text.length }} / 2000</span>
      <el-button type="primary" native-type="submit" class="send" :icon="sending ? undefined : Promotion" :loading="sending"
        :disabled="!canSend">{{ sending ? '正在处理…' : '发送' }}</el-button>
    </div>
  </form>
</template>

<style scoped>
.composer { display: flex; flex-direction: column; gap: 10px; padding: 12px 14px 14px; border-top: 1px solid #efe9e3; background: var(--app-surface); }
.attachment { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border: 1px solid var(--app-border); border-radius: 10px; background: var(--app-surface-muted); }
.attachment-text { display: flex; flex: 1; flex-direction: column; min-width: 0; color: var(--app-text); font-size: 13px; font-weight: 600; text-align: left; }
.attachment-text small { color: var(--app-text-muted); font-size: 12px; font-weight: 400; }
.text {
  width: 100%;
  min-height: 72px;
  max-height: 180px;
  padding: 10px 12px;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  outline: 0;
  background: var(--app-surface);
  color: var(--app-text);
  font: inherit;
  font-size: 14px;
  line-height: 1.6;
  resize: none;
  transition: border-color var(--motion-fast), box-shadow var(--motion-fast);
}
.text:focus { border-color: var(--el-color-primary); box-shadow: 0 0 0 3px var(--tone-peach-bg); }
.text:disabled { background: var(--app-surface-muted); cursor: not-allowed; }
.row { display: flex; align-items: center; gap: 8px; }
.count { flex: 1; color: var(--app-text-muted); font-size: 12px; font-variant-numeric: tabular-nums; text-align: right; }
.picker { display: flex; flex-direction: column; gap: 2px; max-height: 320px; overflow-y: auto; }
.picker-title { margin: 0 0 4px; padding: 0 6px; color: var(--app-text-body); font-size: 12px; font-weight: 600; }
.choice { display: flex; align-items: center; gap: 10px; min-height: 44px; padding: 6px; border: 0; border-radius: 8px; background: transparent; font: inherit; cursor: pointer; }
.choice:hover, .choice:focus-visible { background: var(--app-surface-muted); outline: none; }
.attach-enter-active, .attach-leave-active { transition: opacity var(--motion-base), transform var(--motion-base); }
.attach-enter-from, .attach-leave-to { opacity: 0; transform: translateY(6px); }
</style>
