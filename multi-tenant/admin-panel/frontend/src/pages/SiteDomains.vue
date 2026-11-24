<script setup>
import { computed, reactive } from 'vue'
import { useRoute } from 'vue-router'
import SiteFrame from '../components/SiteFrame.vue'
import { siteByName, sites } from '../data.js'
import { ask } from '../ui.js'
import { job, runAction } from '../useJob.js'
const name = useRoute().params.site, s = computed(() => siteByName(name))
const f = reactive({ domain: '', primary: false, port: '', portPrimary: false })
const info = computed(() => s.value?.domain_info || [])
const hn = computed(() => (s.value?.host_name || '').replace(/\/$/, ''))
const isPrimary = d => {
  const host = hn.value.replace(/^https?:\/\//, '').replace(/[:\/].*$/, '')
  const portUrls = info.value.filter(x => x.port).map(x => x.url)
  return d.port ? hn.value === d.url : host === d.name && !portUrls.includes(hn.value)
}
const httpPort = computed(() => sites.data?.http_port || 80), https = computed(() => !!sites.data?.https)
const canCert = d => !d.cert && !d.is_ip && !d.port && httpPort.value === 80 && https.value
// Say why "Get certificate" is missing.
const why = computed(() => !info.value.some(d => !d.is_ip && !d.cert) ? '' : !https.value ? sites.data.https_reason
  : httpPort.value !== 80 ? `Let's Encrypt checks domains on port 80, but this stack's HTTP_PORT is ${httpPort.value}.` : '')
async function addDomain() {
  const domain = f.domain.trim().toLowerCase()
  if (domain && await runAction('domain_add', { site: name, domain, primary: f.primary })) { f.domain = ''; f.primary = false }
}
async function addPort() {
  const port = String(f.port).trim()
  if (port && await runAction('port_add', { site: name, port, primary: f.portPrimary })) { f.port = ''; f.portPrimary = false }
}
const primary = d => runAction('domain_primary', { site: name, domain: d.port ? String(d.port) : d.name })
const remove = d => d.port
  ? runAction('port_remove', { port: String(d.port) }, `Remove port ${d.port} from ${name}?\n\nhttp://<server-ip>:${d.port} will stop working.`, { action: 'Remove', danger: true })
  : runAction('domain_remove', { site: name, domain: d.name }, `Remove ${d.name} from ${name}?\n\nRequests for ${d.name} will no longer reach ${name}.`, { action: 'Remove', danger: true })
async function cert(d) {
  const a = await ask(`Get a Let's Encrypt certificate for ${d.name}?\n\n${d.name} must be a public domain whose DNS points at this server, with port 80 open from the internet. Renewal is automatic.`, { action: 'Get certificate', input: 'E-mail for expiry notices (optional)' })
  if (a) runAction('domain_cert', { site: name, domain: d.name, email: a.value.trim() })
}
</script>
<template>
  <SiteFrame>
    <p v-if="why" class="notice"><b>Get certificate</b> is unavailable. {{ why }} <RouterLink v-if="!https" to="/setup/nginx">Open HTTPS setup</RouterLink></p>
    <section class="panel flush"><div class="scroll"><table>
      <thead><tr><th>Address</th><th>HTTPS</th><th></th></tr></thead>
      <tbody><tr v-for="(d, i) in info" :key="d.name + d.port">
        <td><a :href="d.url" target="_blank" rel="noopener"><b>{{ d.name }}</b></a> <Badge v-if="isPrimary(d)" kind="ok">primary</Badge> <Badge v-if="d.port">IP + port</Badge> <Badge v-if="i === 0">site name</Badge></td>
        <td><template v-if="d.port">–</template><Badge v-else-if="d.cert" kind="ok" title="Let's Encrypt, renewed automatically">trusted</Badge><Badge v-else-if="https" title="Browsers warn">self-signed</Badge><template v-else>off</template></td>
        <td><div class="actions">
          <button v-if="!isPrimary(d)" class="btn small" :disabled="job.active" @click="primary(d)">Make primary</button>
          <button v-if="canCert(d)" class="btn small" :disabled="job.active" @click="cert(d)">Get certificate</button>
          <button v-if="i !== 0" class="btn small danger" :disabled="job.active" @click="remove(d)">Remove</button></div></td>
      </tr></tbody></table></div></section>
    <form class="panel" @submit.prevent="addDomain"><h2>Add a domain</h2>
      <p class="hint">nginx sends every request for it to this site. Adding or removing a domain briefly restarts the backend and workers (a few seconds, all sites). Point it at this server too: a DNS A record, or a hosts-file line.</p>
      <div class="inline"><label class="field"><span>Domain</span><input id="dm-new" v-model="f.domain" placeholder="erp.acme.com" autocomplete="off" autocapitalize="off" spellcheck="false"></label><button class="btn primary" :disabled="job.active">Add domain</button></div>
      <label class="check" style="margin-top:12px"><input v-model="f.primary" type="checkbox"><span>Make it primary: the URL in emails and the one PDFs load images from</span></label></form>
    <form class="panel" @submit.prevent="addPort"><h2>Add IP + port</h2>
      <p class="hint">Everything on <code>http://&lt;server-ip&gt;:&lt;port&gt;</code> goes to this site, no DNS needed (HTTP; HTTPS once the site is behind the Authelia login). Browsers share login cookies across ports of one IP, so one browser stays logged in to only one IP + port site at a time.</p>
      <div class="inline"><label class="field"><span>Port</span><input id="dm-port" v-model="f.port" type="number" min="1" max="65535" placeholder="8001"></label><button class="btn primary" :disabled="job.active">Add IP + port</button></div>
      <label class="check" style="margin-top:12px"><input v-model="f.portPrimary" type="checkbox"><span>Make it primary</span></label></form>
  </SiteFrame>
</template>
