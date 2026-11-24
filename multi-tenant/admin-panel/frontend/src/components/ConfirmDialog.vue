<script setup>
import { nextTick, ref, watch } from 'vue'
import { closeDialog, dialog } from '../ui.js'
const el = ref(null)
watch(() => [dialog.open, dialog.done], async () => {
  await nextTick()
  if (dialog.open && !el.value.open) el.value.showModal()
  if (!dialog.open && el.value.open) el.value.close()
})
</script>
<template>
  <dialog ref="el" class="dialog" aria-labelledby="dlg-title" @cancel.prevent="closeDialog(false)" @click.self="closeDialog(false)">
    <form v-if="dialog.open" method="dialog" @submit.prevent="closeDialog(true)">
      <h2 id="dlg-title">{{ dialog.title }}</h2>
      <p v-if="dialog.body" class="pre">{{ dialog.body }}</p>
      <label v-if="dialog.input" class="field"><span>{{ dialog.input }}</span><input v-model="dialog.value" autofocus></label>
      <div class="dialog-actions">
        <button type="button" class="btn" :autofocus="!dialog.input" @click="closeDialog(false)">Cancel</button>
        <button class="btn" :class="dialog.danger ? 'danger-solid' : 'primary'" data-ok>{{ dialog.action }}</button>
      </div>
    </form>
  </dialog>
</template>
