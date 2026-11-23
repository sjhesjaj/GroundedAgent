<script setup lang="ts">
// A product tile. The demo data has no product images, so a tinted tile with a
// line icon for the category stands in for one.
import { computed } from 'vue'

const props = withDefaults(defineProps<{ category?: string, size?: number }>(), { category: '', size: 64 })

type Icon = 'mug' | 'kettle' | 'phones' | 'shirt' | 'briefs' | 'box'

const ICONS: Record<string, Icon> = { 家居: 'mug', 家电: 'kettle', 数码: 'phones', 服装: 'shirt', 贴身衣物: 'briefs' }
const icon = computed<Icon>(() => ICONS[props.category] ?? 'box')
const box = computed(() => ({ width: `${props.size}px`, height: `${props.size}px`, borderRadius: `${Math.round(props.size / 6.4)}px` }))
const glyph = computed(() => `${Math.round(props.size * 0.47)}px`)
</script>

<template>
  <span class="thumb" :class="`thumb-${icon}`" :style="box" aria-hidden="true">
    <svg :width="glyph" :height="glyph" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
      stroke-linecap="round" stroke-linejoin="round">
      <template v-if="icon === 'mug'">
        <path d="M5 8h11v7a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4z" /><path d="M16 10h1.5a2.5 2.5 0 0 1 0 5H16" />
        <path d="M8.5 3.5c0 1 1 1.2 1 2.2" /><path d="M12 3.5c0 1 1 1.2 1 2.2" />
      </template>
      <template v-else-if="icon === 'kettle'">
        <path d="M6 20h12l-1.5-10h-9z" /><path d="M8 10a4 4 0 0 1 8 0" /><path d="M12 4.5V6" /><path d="M7 13L3.5 10" />
      </template>
      <template v-else-if="icon === 'phones'">
        <path d="M4 15v-3a8 8 0 0 1 16 0v3" /><path d="M4 14h3v6H5a1 1 0 0 1-1-1z" /><path d="M20 14h-3v6h2a1 1 0 0 0 1-1z" />
      </template>
      <template v-else-if="icon === 'shirt'">
        <path d="M8.5 3.5L4 6l1.8 4 2.2-1v11h8V9l2.2 1L20 6l-4.5-2.5c-.6 1.6-1.9 2.5-3.5 2.5s-2.9-.9-3.5-2.5z" />
      </template>
      <template v-else-if="icon === 'briefs'">
        <path d="M4 6h16l-.8 4.5c-3.2.8-5.2 3.6-5.2 7.5h-4c0-3.9-2-6.7-5.2-7.5z" /><path d="M4.3 8.5h15.4" />
      </template>
      <template v-else>
        <path d="M3 7l9-4 9 4-9 4z" /><path d="M3 7v10l9 4 9-4V7" /><path d="M12 11v10" />
      </template>
    </svg>
  </span>
</template>

<style scoped>
.thumb { display: grid; place-items: center; flex: none; }
.thumb-mug { background: #f6efe6; color: #8a5a26; }
.thumb-kettle { background: #eaf5ef; color: #2a7350; }
.thumb-phones { background: #efecf9; color: #5b45a3; }
.thumb-shirt { background: #eaf1fa; color: #2f5f9e; }
.thumb-briefs { background: #fbedf1; color: #a23d63; }
.thumb-box { background: #f0ece8; color: #6b625c; }
</style>
