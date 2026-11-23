import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import Components from 'unplugin-vue-components/vite'
import { defineConfig } from 'vitest/config'

// Element Plus is imported on demand: a component used in a template (and
// ElMessage / ElMessageBox in a script) brings in only its own code and CSS.
// The generated declaration files are committed so that vue-tsc sees them.
// Tests take the components without their stylesheets (jsdom renders no CSS),
// so Node loads Element Plus as it is instead of Vite transforming all of it.
const elementPlus = ElementPlusResolver({ importStyle: process.env.VITEST ? false : 'css' })

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({ resolvers: [elementPlus], dts: 'src/auto-imports.d.ts' }),
    Components({ resolvers: [elementPlus], dts: 'src/components.d.ts', dirs: [] }),
  ],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8000', '/auth': 'http://127.0.0.1:8000' },
  },
  test: {
    include: ['tests/**/*.test.ts'],
    environment: 'node',   // component tests opt in to jsdom with a file comment
    restoreMocks: true,
    unstubGlobals: true,
    unstubEnvs: true,
  },
})
