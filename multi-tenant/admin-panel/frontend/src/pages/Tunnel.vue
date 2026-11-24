<script setup>
import { computed, reactive, watch } from 'vue'
import { wg, usePoll } from '../data.js'
import { job, runAction } from '../useJob.js'
import { toast } from '../ui.js'
usePoll(wg.load, 15000)
const KEYS = ['endpoint', 'port', 'subnet'], f = reactive({ endpoint: '', port: '', subnet: '' })
let seen = null
// A field follows the server until it is edited.
watch(() => wg.data, w => {
  if (!w) return
  for (const k of KEYS) if (!seen || f[k] === (seen[k] || '')) f[k] = w[k] || ''
  if (w.peers.length) f.subnet = w.subnet
  seen = w
}, { immediate: true })
const facts = computed(() => { const w = wg.data; return [['Print servers dial', w.endpoint ? w.endpoint + ':' + w.port : 'not set'], ['Tunnel network', w.subnet], ['This server', w.hub], ['Connected', w.peers.filter(p => p.up).length + ' of ' + w.peers.length]] })
function save() {
  const w = wg.data, body = Object.fromEntries(KEYS.map(k => [k, f[k].trim()]))
  if (!body.endpoint) return toast('Enter the public IP or DNS name of this server.', 'error')
  let q = w.installed ? 'Apply and restart the tunnel?\n\nPrinting pauses for a few seconds.' : 'Install WireGuard on this server now?'
  if (w.installed && w.peers.length && (body.endpoint !== w.endpoint || body.port !== w.port)) q += '\n\nThe address or port changes: print servers may stay disconnected until you run their setup scripts again.'
  runAction('install_wireguard', body, q, { action: w.installed ? 'Save and repair' : 'Install WireGuard' })
}
</script>
<template>
  <div class="view">
    <PageHead title="WireGuard tunnel" lead="Lets the ERP containers print to CUPS servers behind shop routers. Print servers cannot open connections to this server or to each other."><Badge v-if="wg.data" :kind="wg.data.installed && wg.data.running ? 'ok' : 'crit'">{{ !wg.data.installed ? 'not installed' : wg.data.running ? 'running' : 'down' }}</Badge></PageHead>
    <p v-if="wg.error" class="notice">{{ wg.error }}</p>
    <template v-if="wg.data">
      <p v-if="!wg.data.stack" class="notice">Install the stack and tools first. <RouterLink to="/setup/stack">Open setup</RouterLink></p>
      <p v-else-if="!wg.data.script" class="notice">install-wireguard.sh is not in the checkout: <RouterLink to="/updates">update the panel and tools</RouterLink> first.</p>
      <p v-else-if="wg.data.installed && !wg.data.tools" class="notice">The tools are missing. <RouterLink to="/setup/tools">Open setup</RouterLink></p>
      <p v-else-if="wg.data.installed && !wg.data.running" class="notice crit">The tunnel is down, so nothing prints through it. Use <b>Save and repair</b> below.</p>
      <section v-if="wg.data.installed" class="panel"><dl class="facts"><div v-for="[k, v] in facts" :key="k"><dt>{{ k }}</dt><dd>{{ v }}</dd></div></dl></section>
      <form v-if="wg.data.stack && wg.data.script" class="panel form" style="max-width:none" @submit.prevent="save"><h2>Settings</h2>
        <label class="field" style="max-width:420px"><span>Public IP or DNS name of this server</span><input id="s-endpoint" v-model="f.endpoint" placeholder="erp.example.com" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
        <label class="field" style="max-width:420px"><span>UDP port</span><input id="s-port" v-model="f.port" inputmode="numeric" autocomplete="off"></label>
        <label class="field" style="max-width:420px"><span>Tunnel network (a /24)</span><input id="s-subnet" v-model="f.subnet" autocomplete="off" :disabled="wg.data.peers.length > 0"></label>
        <p v-if="wg.data.peers.length" class="hint">The network is fixed while print servers exist. After changing the address or port, run each print server's setup script again.</p>
        <button class="btn primary" :disabled="job.active">{{ wg.data.installed ? 'Save and repair' : 'Install WireGuard' }}</button></form>
    </template>
  </div>
</template>
