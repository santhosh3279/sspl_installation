<script setup>
import { computed, ref, watch } from 'vue'
import { ask } from '../ui.js'
import { job, runAction } from '../useJob.js'
const props = defineProps({ p: Object })
const domain = ref(props.p.domain)
watch(() => props.p.domain, d => { domain.value = d })
const d = computed(() => domain.value.trim().toLowerCase()), same = computed(() => d.value === props.p.domain)
const usable = computed(() => props.p.tools && props.p.https)
const OPEN = 'The panel is then reachable wherever nginx is, with only its own login. Use a strong admin password.'
const save = () => { if (d.value && !same.value) runAction('panel_domain_set', { domain: d.value }, `Open the admin panel on ${d.value}?\n\n${OPEN}${props.p.domain ? `\n\n${props.p.domain} stops working and its certificate is deleted.` : ''}`, { action: 'Save domain' }) }
async function cert() {
  const a = await ask(`Get a Let's Encrypt certificate for ${d.value}?\n\n${d.value} must be a public domain whose DNS points at this server, with port 80 open from the internet. Renewal is automatic.${same.value ? '' : `\n\nThis also makes it the panel's domain. ${OPEN}${props.p.domain ? ` ${props.p.domain} stops working and its certificate is deleted.` : ''}`}`, { action: 'Get certificate', input: 'E-mail for expiry notices (optional)' })
  if (a) runAction('panel_domain_cert', { domain: d.value, email: a.value.trim() })
}
const remove = () => runAction('panel_domain_remove', {}, `Remove ${props.p.domain} from the panel?\n\nIts certificate is deleted. The panel stays on port ${props.p.port} of this server.`, { action: 'Remove domain', danger: true })
</script>
<template>
  <section class="panel"><h2>Admin panel domain</h2><p class="hint">Open this panel on its own name over HTTPS. The address with port {{ p.port }} keeps working.</p>
    <p v-if="!p.tools" class="notice" style="margin-bottom:14px">Refresh the tools first.</p>
    <p v-else-if="!p.https" class="notice" style="margin-bottom:14px">{{ p.https_reason }} <RouterLink to="/setup/nginx">Open HTTPS setup</RouterLink></p>
    <p v-if="p.domain" class="checks" style="margin-bottom:14px"><a :href="p.url" target="_blank" rel="noopener">{{ p.url }}</a><Badge :kind="p.cert ? 'ok' : 'warn'">{{ p.cert ? "Let's Encrypt, renews automatically" : 'self-signed' }}</Badge></p>
    <form class="inline" @submit.prevent="save"><label class="field"><span>Domain</span><input v-model="domain" placeholder="admin.example.com" autocomplete="off" autocapitalize="none" spellcheck="false" :disabled="!usable"></label>
      <button class="btn" :disabled="job.active || !usable || !d || same">Save domain</button>
      <button type="button" class="btn primary" :disabled="job.active || !usable || !d || p.http_port !== 80 || (same && p.cert)" @click="cert">Get Let's Encrypt certificate</button>
      <button v-if="p.domain" type="button" class="btn danger" :disabled="job.active || !p.tools" @click="remove">Remove</button></form>
    <p v-if="usable && p.http_port !== 80" class="hint" style="margin-top:12px">Let's Encrypt checks the domain on port 80, but this server's HTTP port is {{ p.http_port }}. Change it on the <RouterLink to="/setup/stack">ERPNext stack</RouterLink> page.</p></section>
</template>
