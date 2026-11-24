<script setup>
import { computed, reactive, ref } from 'vue'
import { getJSON, postJSON } from '../api.js'
import { usePoll } from '../data.js'
import { ask } from '../ui.js'
const d = ref(null), size = ref(''), m = reactive({ text: '', bad: false }), busy = ref(false)
const say = (t, bad = false) => { m.text = t; m.bad = bad }
const show = x => { d.value = x; if (x.error) say(x.error, true) }
let first = true
usePoll(async () => {
  const q = first ? '?fresh=1' : ''
  first = false
  try { const x = await getJSON('/api/db-memory' + q); if (!busy.value) show(x) } catch { if (!busy.value) say('Could not load. What you see may be out of date.', true) }
}, 30000)
const bytes = v => parseInt(v, 10) * (v.toUpperCase().endsWith('G') ? 1073741824 : 1048576)
async function send(url, body, wait, done) {
  busy.value = true; say(wait)
  try { const j = await postJSON(url, body); if (j.error) say(j.error, true); else { say(j.message); show(j.state); done?.() } } catch { say('The request failed.', true) }
  busy.value = false
}
async function apply(v) {
  const s = d.value
  const warn = s.live && bytes(v) - s.live > s.available ? `\n\nWARNING: this needs ${v} but only ${s.h.available} of RAM is free. The pool would push other programs into swap and make the server slower. Free RAM first (e.g. restart OpenWA), or pick a smaller size.` : ''
  if (await ask(`Change the database buffer pool from ${s.h.live} to ${v}?\n\nIt applies at once, with no restart.${warn}`, { action: 'Apply size', danger: !!warn })) send('/api/db-memory', { size: v }, 'Applying…', () => { size.value = '' })
}
function set() {
  const v = size.value.trim().toUpperCase()
  if (!/^\d+[MG]$/.test(v)) return say('Enter a size like 2G or 1536M', true)
  apply(v)
}
async function persist() {
  if (await ask('Make the buffer pool size permanent?\n\nThis adds the MariaDB settings mount to the compose file and RECREATES THE DATABASE CONTAINER. Every site loses its database for about 10-30 seconds. Do this when nobody is billing.', { action: 'Make permanent', danger: true })) send('/api/db-memory/persist', {}, 'Recreating the db container… (up to a minute)')
}
const facts = computed(() => { const s = d.value; return [['Pool size now', s.h.live], ['Saved setting', s.h.configured], ['Database size', s.h.data], ['Recommended', s.h.recommended], ['Reads from disk (recent)', s.recent_miss_pct === null ? 'measuring…' : s.recent_miss_pct + '%'], ['Reads from disk (since DB start)', s.miss_pct === null ? '–' : s.miss_pct + '%'], ['Survives a DB restart', s.mounted ? 'yes' : 'no, falls back to 128M'], ['RAM free on the server', s.h.available]] })
</script>
<template>
  <div class="view">
    <PageHead title="Database memory" lead="The buffer pool is the RAM MariaDB keeps database pages in. When it is smaller than the database, queries read from disk and every bill save slows down." />
    <p v-if="m.text" class="notice" :class="m.bad ? 'crit' : 'ok'" role="status">{{ m.text }}</p>
    <template v-if="d">
      <section class="panel"><dl class="facts"><div v-for="[k, v] in facts" :key="k"><dt>{{ k }}</dt><dd>{{ v }}</dd></div></dl></section>
      <form class="panel" @submit.prevent="set"><h2>Change the size</h2><p class="hint">A new size applies at once, with no restart. The allowed range is {{ d.h.min }} – {{ d.h.max }}: the server keeps 2G for everything else. Raise it only when there is free RAM, because a pool pushed into swap is slower than a small one.</p>
        <div class="inline"><label class="field"><span>Size</span><input id="d-size" v-model="size" :placeholder="'e.g. ' + d.h.recommended" autocomplete="off"></label><button class="btn primary" :disabled="busy">Apply size</button><button type="button" class="btn" :disabled="busy" @click="apply(d.h.recommended)">Use recommended</button></div></form>
      <section v-if="!d.mounted" class="panel edge-danger"><h2>Make it permanent</h2><p class="hint">Without this, the pool falls back to 128M on the next database restart.</p><button class="btn danger" :disabled="busy" @click="persist">Make permanent (restarts DB)</button></section>
    </template>
  </div>
</template>
