import { reactive } from 'vue'

// ---- toasts
export const toasts = reactive([])
let nextId = 1
export function toast(text, kind = 'ok') {
  toasts.splice(0)                     // one message: a pile would cover the page
  const id = nextId++
  toasts.push({ id, text, kind })
  if (kind !== 'error') setTimeout(() => dismiss(id), 6000)
}
export function dismiss(id) {
  const i = toasts.findIndex(t => t.id === id)
  if (i >= 0) toasts.splice(i, 1)
}

// ---- confirmation. First paragraph = title, the rest = body.
// Resolves to false, or true (the typed text when `input` is given).
export const dialog = reactive({ open: false, title: '', body: '', action: '', danger: false, input: null, value: '', done: null })
export function ask(text, { action = 'Continue', danger = false, input = null } = {}) {
  const [title, ...rest] = text.split('\n\n')
  dialog.done?.(false)
  return new Promise(resolve => {
    Object.assign(dialog, { open: true, title, body: rest.join('\n\n'), action, danger, input, value: '', done: resolve })
  })
}
export function closeDialog(ok) {
  const { done, input, value } = dialog
  dialog.open = false
  dialog.done = null
  done?.(ok ? (input ? { value } : true) : false)
}
