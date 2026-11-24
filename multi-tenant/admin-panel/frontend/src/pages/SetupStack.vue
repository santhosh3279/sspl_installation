<script setup>
import { reactive, watch } from 'vue'
import SetupFrame from '../components/SetupFrame.vue'
import { setup } from '../data.js'
import { job, runAction } from '../useJob.js'
const f = reactive({ image: '', db: '', cron: true })
let seeded = false
watch(() => setup.data, s => { if (s?.repo_ok && !seeded) { f.image = s.erp_image || ''; seeded = true } }, { immediate: true })
async function install() {
  if (await runAction('install_stack', { image: f.image.trim(), db_password: f.db, schedule_cron: f.cron }, `Install the multi-tenant ERPNext stack now?\n\nThis pulls the image, starts the containers and installs the tools to ${setup.data.tools_dir}. 5-15 minutes. No site is created: add sites afterwards.`, { action: 'Install stack' })) f.db = ''
}
const ports = () => runAction('install_ports', {}, 'Move every site to ports 80 and 443?\n\nDomains then open without a port. Links with the old port stop working, and both ports must be free.', { action: 'Use 80 and 443' })
</script>
<template>
  <SetupFrame title="ERPNext stack" lead="One stack, many sites. The containers that run ERPNext on this server.">
    <template #state="{ c }"><Badge :kind="c.erp.installed ? 'ok' : 'warn'">{{ c.erp.installed ? 'installed' : 'not installed' }}</Badge><Badge v-if="c.erp.installed && c.erp.running" kind="ok">running</Badge></template>
    <template #default="{ s, c }">
      <form v-if="!c.erp.installed" class="panel form" style="max-width:none" @submit.prevent="install">
        <p class="hint">Creates the stack only. Then create a site for each tenant: each gets its own hostname, database and Administrator password. Ports 80 and 443 must be free, so every domain opens without a port.</p>
        <label class="field" style="max-width:480px"><span>Docker image</span><input id="erp-image" v-model="f.image"><small>Registry/owner/name, no tag. One image serves every site.</small></label>
        <label class="field" style="max-width:480px"><span>MariaDB root password (new)</span><input id="erp-db" v-model="f.db" type="password" autocomplete="new-password"></label>
        <label class="check"><input id="erp-cron" v-model="f.cron" type="checkbox"><span>Schedule backups for all sites</span></label>
        <button class="btn primary" :disabled="job.active">Install stack</button></form>
      <template v-else>
        <section class="panel"><dl class="facts"><div><dt>HTTP port</dt><dd>{{ c.erp.http_port }}</dd></div><div><dt>Sites</dt><dd>{{ c.erp.sites.length }}</dd></div><div><dt>Tools</dt><dd>{{ c.tools.installed ? 'installed' : 'not installed' }}</dd></div></dl>
          <p class="hint" style="margin-top:16px"><RouterLink to="/sites">{{ c.erp.sites.length ? 'Manage the sites' : 'Create the first site' }}</RouterLink></p></section>
        <section v-if="s.scripts.ports && (c.erp.http_port !== 80 || (c.proxy.https_port !== null && c.proxy.https_port !== 443))" class="panel"><h2>Remove the port from URLs</h2><p class="hint">Domains need a port in the URL because this stack is not on the standard ones (HTTP {{ c.erp.http_port }}<template v-if="c.proxy.https_port !== null">, HTTPS {{ c.proxy.https_port }}</template>).</p><button class="btn primary" :disabled="job.active" @click="ports">Use ports 80 and 443</button></section>
      </template>
    </template>
  </SetupFrame>
</template>
