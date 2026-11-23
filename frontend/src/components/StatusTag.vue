<script setup lang="ts">
import type { Tone } from '@/utils/outcome'

withDefaults(defineProps<{ tone?: Tone, pulse?: boolean }>(), { tone: 'gray', pulse: false })
</script>

<template>
  <span class="status-tag" :class="`tone-${tone}`"><i :class="{ pulse }" aria-hidden="true"></i><slot /></span>
</template>

<style scoped>
.status-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 10px;
  border-radius: 12px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  transition: background-color var(--motion-base), color var(--motion-base);
}
.status-tag i { width: 6px; height: 6px; border-radius: 3px; flex: none; }
.status-tag i.pulse { animation: app-pulse 1.6s ease-in-out infinite; }
.tone-amber { color: var(--tone-amber); background: var(--tone-amber-bg); }
.tone-amber i { background: var(--tone-amber-dot); }
.tone-green { color: var(--tone-green); background: var(--tone-green-bg); }
.tone-green i { background: var(--tone-green-dot); }
.tone-red { color: var(--tone-red); background: var(--tone-red-bg); }
.tone-red i { background: var(--tone-red-dot); }
.tone-blue { color: var(--tone-blue); background: var(--tone-blue-bg); }
.tone-blue i { background: var(--tone-blue-dot); }
.tone-gray { color: var(--tone-gray); background: var(--tone-gray-bg); }
.tone-gray i { background: var(--tone-gray-dot); }
.tone-peach { color: var(--tone-peach); background: var(--tone-peach-bg); }
.tone-peach i { background: var(--tone-peach-dot); }
</style>
