<script setup lang="ts">
// The white bar on top of a signed-in page: brand, page name, the demo's
// business time, and the account menu (退出登录).
import { ArrowDown, Clock, SwitchButton } from '@element-plus/icons-vue'
import { computed } from 'vue'

import type { AuthUser } from '@/types'

const props = defineProps<{
  page: string
  user: AuthUser
  /** Display form ("2026-11-15 10:00"); hidden while unknown. */
  businessTime?: string
  /** The avatar letter; by default the last character of the display name. */
  initial?: string
}>()

const emit = defineEmits<{ logout: [] }>()

const letter = computed(() => props.initial || [...props.user.display_name].pop() || '我')

function onCommand(command: string) {
  if (command === 'logout') emit('logout')
}
</script>

<template>
  <header class="top-bar">
    <div class="inner">
      <div class="brand">
        <span class="mark" aria-hidden="true">G</span>
        <strong>GroundedAgent</strong>
        <span class="page">{{ page }}</span>
      </div>
      <div class="middle"><slot /></div>
      <span v-if="businessTime" class="time" title="演示环境的业务时间">
        <el-icon aria-hidden="true"><Clock /></el-icon>业务时间 {{ businessTime }}
      </span>
      <el-dropdown trigger="click" placement="bottom-end" @command="onCommand">
        <button type="button" class="account" :aria-label="`账号菜单：${user.display_name}`">
          <span class="avatar" aria-hidden="true">{{ letter }}</span>
          <span class="name">{{ user.display_name }}</span>
          <el-icon class="caret" aria-hidden="true"><ArrowDown /></el-icon>
        </button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item disabled>{{ user.username }}</el-dropdown-item>
            <el-dropdown-item command="logout" divided :icon="SwitchButton">退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </div>
  </header>
</template>

<style scoped>
.top-bar {
  position: sticky;
  top: 0;
  z-index: 20;
  height: 64px;
  background: var(--app-surface);
  border-bottom: 1px solid var(--app-border);
}
.inner {
  display: flex;
  align-items: center;
  gap: 16px;
  max-width: 1440px;
  height: 100%;
  margin: 0 auto;
  padding: 0 32px;
}
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.mark {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 18px;
  font-weight: 700;
  flex: none;
}
.brand strong { font-size: 16px; }
.page { padding-left: 12px; border-left: 1px solid var(--app-border); color: var(--app-text-body); font-size: 14px; white-space: nowrap; }
.middle { flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; }
.time {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border-radius: 16px;
  background: var(--app-bg);
  color: var(--app-text-body);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.account {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 40px;
  padding: 0 10px 0 4px;
  border: 1px solid var(--app-border);
  border-radius: 20px;
  background: var(--app-surface);
  color: var(--app-text);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
  transition: border-color var(--motion-fast);
}
.account:hover { border-color: #cfc6be; }
.account:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.avatar {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 16px;
  background: var(--tone-peach-bg);
  color: var(--el-color-primary-dark-2);
  font-size: 14px;
  font-weight: 700;
}
.caret { color: var(--app-text-muted); }

@media (max-width: 900px) {
  .inner { padding: 0 16px; gap: 10px; }
  .time, .name, .brand strong { display: none; }
  .page { padding-left: 0; border-left: 0; font-size: 16px; font-weight: 600; color: var(--app-text); }
}
</style>
