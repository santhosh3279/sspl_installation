<script setup>
import { computed } from 'vue'
import { stats, usePoll } from '../data.js'
usePoll(stats.load, 5000)
const c = computed(() => stats.data?.containers)
</script>
<template>
  <div class="view">
    <PageHead title="Containers" lead="The Docker services that run ERPNext." />
    <p v-if="stats.error || c?.error" class="notice crit">{{ stats.error || c.error }}</p>
    <section class="panel flush"><div class="scroll"><table><thead><tr><th>Service</th><th>State</th><th>Status</th></tr></thead><tbody><tr v-for="x in c?.services" :key="x.name"><td><b>{{ x.name }}</b></td><td><i class="dot" :class="{ up: x.state === 'running' }"></i>{{ x.state }}</td><td>{{ x.status }}</td></tr><tr v-if="!c?.services.length"><td colspan="3" class="hint">{{ c ? 'No containers reported.' : 'Loading…' }}</td></tr></tbody></table></div></section>
  </div>
</template>
