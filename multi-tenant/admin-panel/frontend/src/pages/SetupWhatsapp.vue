<script setup>
import { onUnmounted, ref } from 'vue'
import SetupFrame from '../components/SetupFrame.vue'
import { postJSON } from '../api.js'
import { job, runAction } from '../useJob.js'
const key = ref(''), msg = ref(''), box = ref(null)
let timer
const url = (s, o) => 'http://' + (s.server_ip || location.hostname) + ':' + o.port
const install = o => runAction('install_openwa', {}, `Install OpenWA now?\n\nThis clones OpenWA, pulls its Docker image and starts it on port ${o.port}. A few minutes.`, { action: 'Install OpenWA' })
// Fetched on demand only, and hidden again so it is not left on screen.
async function show() {
  msg.value = 'Reading…'
  try {
    const j = await postJSON('/api/openwa/key', {})
    if (j.error) { msg.value = j.error; return }
    key.value = j.key; msg.value = ''
    clearTimeout(timer); timer = setTimeout(() => { key.value = '' }, 30000)
  } catch { msg.value = 'The request failed.' }
}
async function copy() {
  try { await navigator.clipboard.writeText(key.value) } catch { box.value.select(); document.execCommand('copy') }
  msg.value = 'Copied.'
}
onUnmounted(() => clearTimeout(timer))
</script>
<template>
  <SetupFrame title="WhatsApp API" lead="OpenWA sends WhatsApp messages for ERPNext.">
    <template #state="{ c }"><Badge :kind="c.openwa.installed ? 'ok' : 'warn'">{{ c.openwa.installed ? 'installed' : 'not installed' }}</Badge><Badge v-if="c.openwa.installed" :kind="c.openwa.running ? 'ok' : 'crit'">{{ c.openwa.running ? 'running' : 'stopped' }}</Badge></template>
    <template #default="{ s, c }">
      <section v-if="!c.openwa.installed" class="panel"><button v-if="s.scripts.openwa" class="btn primary" :disabled="job.active" @click="install(c.openwa)">Install OpenWA</button><p v-else class="hint">The OpenWA installer is not in the checkout.</p></section>
      <template v-else><section class="panel"><dl class="facts"><div><dt>Dashboard and API</dt><dd><a :href="url(s, c.openwa)" target="_blank" rel="noopener">{{ url(s, c.openwa) }}</a></dd></div><div><dt>API reference</dt><dd><a :href="url(s, c.openwa) + '/api/docs'" target="_blank" rel="noopener">Swagger</a></dd></div></dl></section>
        <section class="panel"><h2>API key</h2><p class="hint">Use it to sign in to the dashboard and as the <code>X-API-Key</code> header on API calls. It hides again after 30 seconds.</p><div class="inline"><input v-if="key" id="ow-key" ref="box" readonly :value="key" aria-label="API key"><button v-if="key" class="btn" @click="copy">Copy</button><button v-else class="btn" :disabled="!c.openwa.running" @click="show">Show API key</button><span class="hint" role="status">{{ msg }}</span></div></section></template>
    </template>
  </SetupFrame>
</template>
