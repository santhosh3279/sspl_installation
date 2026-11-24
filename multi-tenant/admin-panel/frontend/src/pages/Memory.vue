<script setup>
import { computed, ref } from 'vue'
import { CLEAR_RAM, clearRam, ram, usePoll } from '../data.js'
import { ask } from '../ui.js'
usePoll(ram.load, 10000)
const s = computed(() => ram.data), msg = ref('')
const level = p => p >= 92 ? 'crit' : p >= 80 ? 'warn' : ''
async function clear() {
  if (!await ask(CLEAR_RAM, { action: 'Clear RAM caches' })) return
  await clearRam(t => { msg.value = t })
  ram.load()
}
</script>
<template>
  <div class="view">
    <PageHead title="Memory"><button class="btn" @click="clear">Clear RAM caches</button></PageHead>
    <p v-if="msg" class="notice ok" role="status">{{ msg }}</p><p v-if="ram.error" class="notice">{{ ram.error }}</p>
    <template v-if="s">
      <div class="stack-list"><div v-for="a in s.alerts" :key="a.text" class="notice" :class="a.level">{{ a.text }}</div><div v-if="!s.alerts.length" class="notice ok">All clear: RAM, swap, containers and the database are healthy.</div></div>
      <section class="panel"><dl class="facts">
        <div><dt>RAM available</dt><dd>{{ s.mem.available }}<small>of {{ s.mem.total }}</small></dd></div>
        <div><dt>RAM used</dt><dd>{{ s.mem.used_pct }}%<small>{{ s.mem.cached }} is reclaimable cache</small></dd></div>
        <div><dt>Swap used</dt><dd>{{ s.swap.total === '0 B' ? 'no swap' : s.swap.used }}<small v-if="s.swap.total !== '0 B'">{{ s.swap.pct }}% of {{ s.swap.total }}</small></dd></div>
        <div><dt>Swapping in / out</dt><dd>{{ s.swap.in }} / {{ s.swap.out }}<small>since the last refresh</small></dd></div>
        <div><dt>Memory pressure</dt><dd>{{ s.psi === null ? 'n/a' : s.psi.toFixed(1) + '%' }}<small>time tasks waited on RAM (10s)</small></dd></div></dl></section>
      <p v-if="s.containers.error" class="notice crit">{{ s.containers.error }}</p>
      <section class="panel flush"><div class="panel-top"><h2>Containers by memory</h2></div><div class="scroll"><table><thead><tr><th>Container</th><th class="r">Memory</th><th class="r">Limit</th><th>Of limit</th><th class="r">Of host RAM</th><th class="r">CPU</th></tr></thead><tbody><tr v-for="c in s.containers.rows" :key="c.name"><td>{{ c.name }}</td><td class="r">{{ c.used_h }}</td><td class="r">{{ c.limit_h }}</td><td><span v-if="c.pct === null" class="hint">no limit</span><div v-else class="track" :title="c.pct + '%'"><i :class="level(c.pct)" :style="{ width: Math.min(c.pct, 100) + '%' }"></i></div></td><td class="r">{{ c.host_pct }}%</td><td class="r">{{ c.cpu }}</td></tr><tr v-if="!s.containers.rows.length"><td colspan="6" class="hint">No running containers.</td></tr></tbody></table></div></section>
      <section class="panel flush"><div class="panel-top"><h2>Top processes</h2></div><div class="scroll"><table><thead><tr><th class="r">PID</th><th>Name</th><th class="r">RAM</th><th class="r">Swap</th><th>Command</th></tr></thead><tbody><tr v-for="p in s.processes" :key="p.pid"><td class="r">{{ p.pid }}</td><td>{{ p.name }}</td><td class="r">{{ p.rss_h }}</td><td class="r">{{ p.swap ? p.swap_h : '–' }}</td><td><div class="cmd" :title="p.cmd">{{ p.cmd }}</div></td></tr></tbody></table></div></section>
    </template>
  </div>
</template>
