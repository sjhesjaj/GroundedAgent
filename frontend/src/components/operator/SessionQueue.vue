<script setup lang="ts">
// The left column of the approval desk: 待审批 / 进行中 / 全部, one row per session.
import { computed } from 'vue'

import StatusTag from '@/components/StatusTag.vue'
import type { OperatorSessionSummary } from '@/types'
import { QUEUE_TABS, queueOf, queueTag, type QueueTab } from '@/utils/desk'
import { shortId } from '@/utils/format'

const props = withDefaults(defineProps<{
  sessions: OperatorSessionSummary[]
  selectedId?: string
  loading?: boolean
  disabled?: boolean
}>(), { selectedId: '', loading: false, disabled: false })

const tab = defineModel<QueueTab>('tab', { default: 'waiting' })
const emit = defineEmits<{ select: [sessionId: string] }>()

const counts = computed(() => Object.fromEntries(QUEUE_TABS.map(({ key }) => [key, queueOf(props.sessions, key).length])))
const rows = computed(() => queueOf(props.sessions, tab.value).map((item) => ({
  item,
  tag: queueTag(item),
  name: item.persona?.display_name ?? '无法读取的会话',
  initial: [...(item.persona?.display_name ?? '?')].pop(),
})))
const emptyText = computed(() => {
  if (!props.sessions.length) return '暂无会话。顾客发起会话后会出现在这里。'
  return tab.value === 'waiting' ? '没有待审批的申请。' : '这个分类下没有会话。'
})
</script>

<template>
  <nav class="queue" aria-label="会话队列">
    <div class="tabs" role="tablist">
      <button v-for="{ key, label } in QUEUE_TABS" :key="key" type="button" role="tab" class="tab"
        :class="{ on: tab === key }" :aria-selected="tab === key" @click="tab = key">
        {{ label }}<b>{{ counts[key] }}</b>
      </button>
    </div>
    <div class="list">
      <template v-if="loading && !sessions.length">
        <el-skeleton v-for="index in 3" :key="index" class="row-skeleton" animated :rows="1" />
      </template>
      <TransitionGroup v-else name="row" tag="div" class="rows">
        <button v-for="({ item, tag, name, initial }, index) in rows" :key="item.session_id" type="button" class="row"
          :class="{ on: item.session_id === selectedId }" :aria-current="item.session_id === selectedId ? 'true' : undefined"
          :data-session="item.session_id" :disabled="disabled" :style="{ '--delay': `${index * 40}ms` }"
          @click="emit('select', item.session_id)">
          <span class="avatar" aria-hidden="true">{{ initial }}</span>
          <span class="main">
            <span class="line"><strong>{{ name }}</strong><StatusTag :tone="tag.tone" :pulse="tag.pulse">{{ tag.text }}</StatusTag></span>
            <span class="meta" :title="item.session_id">会话 {{ shortId(item.session_id) }}<template v-if="item.message_count !== undefined"> · {{ item.message_count }} 条消息</template></span>
          </span>
        </button>
      </TransitionGroup>
      <p v-if="!loading && !rows.length" class="empty">{{ emptyText }}</p>
    </div>
  </nav>
</template>

<style scoped>
.queue { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--app-surface); border-right: 1px solid var(--app-border); }
.tabs { display: flex; gap: 18px; padding: 0 16px; border-bottom: 1px solid #efe9e3; flex: none; white-space: nowrap; }
.tab {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 48px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--app-text-body);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
}
.tab b {
  display: inline-grid;
  place-items: center;
  min-width: 20px;
  height: 20px;
  padding: 0 6px;
  border-radius: 10px;
  background: #f0ece8;
  color: var(--app-text-body);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.tab.on { color: var(--el-color-primary-dark-2); font-weight: 600; }
.tab.on b { background: var(--el-color-primary); color: #fff; }
.tab.on::after { position: absolute; right: 0; bottom: -1px; left: 0; height: 2px; border-radius: 1px; content: ''; background: var(--el-color-primary); }
.tab:focus-visible, .row:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.list { flex: 1; min-height: 0; overflow-y: auto; padding: 10px; }
.rows { display: flex; flex-direction: column; gap: 6px; }
.row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  width: 100%;
  padding: 12px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--app-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: background-color var(--motion-fast), border-color var(--motion-fast), transform var(--motion-fast);
  animation: app-rise var(--motion-base) ease-out both;
  animation-delay: var(--delay);
}
.row:hover:not(:disabled) { background: var(--app-surface-muted); transform: translateY(-1px); }
.row.on, .row.on:hover { background: #fdf6f2; border-color: #efcbbc; transform: none; }
.row:disabled { cursor: progress; }
.avatar { display: grid; place-items: center; width: 36px; height: 36px; border-radius: 18px; background: var(--tone-peach-bg); color: var(--el-color-primary-dark-2); font-size: 14px; font-weight: 700; flex: none; }
.main { display: flex; flex: 1; flex-direction: column; gap: 6px; min-width: 0; }
/* In a narrow column the tag drops under the name instead of squeezing it. */
.line { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 6px 8px; }
.line strong { overflow: hidden; font-size: 14px; text-overflow: ellipsis; white-space: nowrap; }
.meta { color: var(--app-text-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.empty { margin: 0; padding: 40px 20px; color: var(--app-text-muted); font-size: 13px; line-height: 1.7; text-align: center; }
.row-skeleton { padding: 12px; }
.row-move { transition: transform var(--motion-base) var(--motion-ease); }
</style>
