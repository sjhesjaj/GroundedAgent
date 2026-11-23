// The customer and operator pages still use the old dark stylesheet until their
// redesign. It styles :root, body and every button, so it must not stay on the
// new pages: it is added while one of the old pages is shown and removed with it.
import { onBeforeMount, onUnmounted } from 'vue'

import legacyCss from '@/styles/legacy.css?inline'

let users = 0
let element: HTMLStyleElement | null = null

export function useLegacyStyles() {
  onBeforeMount(() => {
    users += 1
    if (users === 1) {
      element = document.createElement('style')
      element.dataset.legacyStyles = ''
      element.textContent = legacyCss
      document.head.append(element)
    }
  })
  onUnmounted(() => {
    users -= 1
    if (users === 0) {
      element?.remove()
      element = null
    }
  })
}
