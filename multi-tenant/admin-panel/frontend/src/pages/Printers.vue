<script setup>
import { reactive, watch } from 'vue'
import { wg, usePoll } from '../data.js'
import { job, runAction } from '../useJob.js'
import { toast } from '../ui.js'
usePoll(wg.load, 15000)
const add = reactive({ name: '', site: '' })
watch(() => wg.data?.sites, s => { if (s && !s.includes(add.site)) add.site = '' })
function ago(s) { return s === null ? 'never' : s < 120 ? s + ' s ago' : s < 7200 ? Math.floor(s / 60) + ' min ago' : s < 172800 ? Math.floor(s / 3600) + ' h ago' : Math.floor(s / 86400) + ' days ago' }
async function addPeer() {
  const name = add.name.trim()
  if (!name) return toast('Enter a name for the print server.', 'error')
  if (await runAction('wg_add', { name, site: add.site })) add.name = ''
}
const remove = name => runAction('wg_remove', { name }, `Remove ${name}?\n\nIts key stops working, printing to it stops, and its address is not used again.`, { action: 'Remove', danger: true })
</script>
<template>
  <div class="view">
    <PageHead title="Print servers" lead="CUPS machines behind shop routers that the ERP containers print to through the WireGuard tunnel."><RouterLink class="btn" to="/tunnel">Tunnel settings</RouterLink></PageHead>
    <p v-if="wg.error" class="notice">{{ wg.error }}</p>
    <template v-if="wg.data">
      <p v-if="!wg.data.installed || !wg.data.tools" class="notice">{{ wg.data.installed ? 'The tools are missing.' : 'WireGuard is not installed.' }} <RouterLink :to="wg.data.installed ? '/setup/tools' : '/tunnel'">Set it up</RouterLink></p>
      <template v-else>
        <p v-if="!wg.data.running" class="notice crit">The tunnel is down, so nothing prints through it. <RouterLink to="/tunnel">Repair it</RouterLink></p>
        <section class="panel flush"><div class="scroll"><table id="peers"><thead><tr><th>Name</th><th>Tunnel address</th><th>Site</th><th>Status</th><th>Last handshake</th><th></th></tr></thead><tbody>
          <tr v-for="p in wg.data.peers" :key="p.name"><td><b>{{ p.name }}</b></td><td><code>{{ p.ip }}</code></td><td>{{ p.site || '–' }}</td><td><Badge :kind="p.up ? 'ok' : 'crit'">{{ p.up ? 'connected' : 'not connected' }}</Badge></td><td>{{ ago(p.age) }}</td><td><div class="actions"><RouterLink v-if="p.up" class="btn small" :to="`/printers/${p.name}/ssh`">SSH</RouterLink><a class="btn small" :href="'/api/wireguard/setup/' + encodeURIComponent(p.name)" title="Contains a private key">Setup script</a><button class="btn small" :disabled="job.active" @click="runAction('wg_test', { name: p.name })">Test CUPS</button><button class="btn small danger" :disabled="job.active" @click="remove(p.name)">Remove</button></div></td></tr>
          <tr v-if="!wg.data.peers.length"><td colspan="6" class="hint">None yet. Add the first one below.</td></tr></tbody></table></div></section>
        <form class="panel" @submit.prevent="addPeer"><h2>Add a print server</h2><p class="hint">The site is a label only: every site on the stack can reach every print server.</p><div class="inline"><label class="field"><span>Name</span><input id="n-name" v-model="add.name" placeholder="shop1" maxlength="31" autocomplete="off" autocapitalize="off" spellcheck="false"></label><label class="field"><span>Site label</span><select id="n-site" v-model="add.site"><option value="">no site label</option><option v-for="s in wg.data.sites" :key="s">{{ s }}</option></select></label><button class="btn primary" :disabled="job.active">Add print server</button></div></form>
        <section class="panel"><h2>Connect it</h2><ol class="steps"><li>Open <b>UDP {{ wg.data.port }}</b> to this server in the cloud firewall or router.</li><li>Download the <b>setup script</b> and run it as root on the CUPS machine (Debian/Ubuntu). It holds a <b>private key</b>: copy it over a safe channel and delete it afterwards.</li><li>Share each printer: <code>lpadmin -p &lt;printer&gt; -o printer-is-shared=true</code></li><li>In ERPNext, set the Printer Server host to the tunnel address above, port 631.</li><li><b>Test CUPS</b> asks for a page from inside the backend container.</li><li><b>SSH</b> shows while a server is connected and needs an SSH server with password login on it. If its firewall (ufw) blocks SSH and it was set up before this feature, download and run its setup script again.</li></ol></section>
      </template>
    </template>
  </div>
</template>
