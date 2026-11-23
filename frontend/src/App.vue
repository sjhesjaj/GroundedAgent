<script setup lang="ts">
// The page frame: Element Plus in Chinese, the routed page, and the way back
// to the login page whenever the account goes away (logout, or any 401).
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

watch(() => auth.user, (user) => {
  if (!user && auth.checked && route.name !== 'login') router.replace({ name: 'login' })
})
</script>

<template>
  <el-config-provider :locale="zhCn">
    <div v-if="!auth.checked" class="boot-screen" role="status">正在检查登录状态…</div>
    <RouterView />
  </el-config-provider>
</template>
