<script setup lang="ts">
// Asked before 批准, 拒绝 and 重置演示数据. Cancelling sends nothing.
import { CircleCheck, Warning } from '@element-plus/icons-vue'

import ProductThumb from '@/components/customer/ProductThumb.vue'
import type { ConfirmCopy } from '@/utils/desk'

defineProps<{
  copy: ConfirmCopy | null
  /** The line the decision is about, when known. */
  subject?: { title: string, detail: string, category?: string } | null
}>()

const open = defineModel<boolean>({ default: false })
const emit = defineEmits<{ confirm: [] }>()
</script>

<template>
  <el-dialog v-model="open" class="confirm" width="min(440px, calc(100vw - 32px))" :show-close="false" align-center
    :append-to-body="true" role="alertdialog">
    <div v-if="copy" class="content">
      <div class="head">
        <span class="icon" :class="{ danger: copy.danger }" aria-hidden="true">
          <el-icon><Warning v-if="copy.danger" /><CircleCheck v-else /></el-icon>
        </span>
        <div>
          <h2 class="title">{{ copy.title }}</h2>
          <p>{{ copy.text }}</p>
        </div>
      </div>
      <div v-if="subject" class="subject">
        <ProductThumb :category="subject.category" :size="44" />
        <div><strong>{{ subject.title }}</strong><span>{{ subject.detail }}</span></div>
      </div>
    </div>
    <template #footer>
      <el-button class="cancel" size="large" @click="open = false">取消</el-button>
      <el-button class="confirm-button" size="large" :type="copy?.danger ? 'danger' : 'primary'" @click="emit('confirm')">
        {{ copy?.confirm }}</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.content { display: flex; flex-direction: column; gap: 16px; }
.head { display: flex; align-items: flex-start; gap: 12px; }
.icon { display: grid; place-items: center; width: 40px; height: 40px; border-radius: 12px; background: var(--tone-peach-bg); color: var(--el-color-primary-dark-2); font-size: 22px; flex: none; }
.icon.danger { background: var(--tone-red-bg); color: var(--tone-red); }
.title { margin: 0; color: var(--app-text); font-size: 18px; }
.head p { margin: 6px 0 0; color: var(--app-text-body); font-size: 14px; line-height: 1.7; }
.subject { display: flex; align-items: center; gap: 12px; padding: 12px; border: 1px solid #efe9e3; border-radius: 10px; background: var(--app-surface-muted); }
.subject div { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.subject strong { font-size: 14px; }
.subject span { color: var(--app-text-muted); font-size: 12px; }
/* Element's default danger red reads as disabled; an irreversible action gets a firm one. */
.confirm-button.el-button--danger {
  --el-button-bg-color: #b42318;
  --el-button-border-color: #b42318;
  --el-button-hover-bg-color: #912018;
  --el-button-hover-border-color: #912018;
  --el-button-active-bg-color: #7a1a12;
  --el-button-active-border-color: #7a1a12;
}
</style>
