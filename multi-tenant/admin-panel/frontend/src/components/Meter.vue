<script setup>
import { computed } from 'vue'
const p = defineProps({ label: String, pct: Number, note: String })
const level = computed(() => p.pct >= 92 ? 'crit' : p.pct >= 80 ? 'warn' : '')
</script>
<template>
  <div class="meter">
    <div class="meter-top"><span>{{ label }}</span><b>{{ pct }}% <template v-if="level">{{ level === 'crit' ? 'critical' : 'high' }}</template></b></div>
    <div class="track"><i :class="level" :style="{ width: Math.min(pct, 100) + '%' }"></i></div>
    <div v-if="note" class="hint">{{ note }}</div>
  </div>
</template>
