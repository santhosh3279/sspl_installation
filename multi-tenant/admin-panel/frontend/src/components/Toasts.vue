<script setup>import { dismiss, toasts } from '../ui.js'
import Icon from './Icon.vue'</script>
<template>
  <div class="toasts" aria-live="polite">
    <div v-for="t in toasts" :key="t.id" class="toast" :class="t.kind" :role="t.kind === 'error' ? 'alert' : null">
      <span>{{ t.text }}</span><button aria-label="Dismiss" @click="dismiss(t.id)"><Icon name="close" /></button>
    </div>
  </div>
</template>
