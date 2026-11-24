<script setup>
import SetupFrame from '../components/SetupFrame.vue'
import { job, runAction } from '../useJob.js'
const fine = p => p.installed && p.running && p.configured && p.frontend_local && (p.https_port === null || p.https_live)
const go = c => runAction('install_proxy', {}, `Install nginx on port ${c.erp.http_port || 80} and route every site's domain through it?\n\nThe stack's own nginx moves to 127.0.0.1. Every site is down for a few seconds.`, { action: c.proxy.installed ? 'Repair nginx routing' : 'Install nginx routing' })
</script>
<template>
  <SetupFrame title="Domains and HTTPS" lead="nginx on the HTTP port maps each domain to its site, so one site can answer on several domains.">
    <template #state="{ c }"><Badge :kind="fine(c.proxy) ? 'ok' : 'warn'">{{ fine(c.proxy) ? 'working' : c.proxy.installed ? 'needs repair' : 'not installed' }}</Badge></template>
    <template #default="{ s, c }">
      <section class="panel"><dl class="facts"><div><dt>nginx</dt><dd>{{ !c.proxy.installed ? 'not installed' : c.proxy.running ? 'running' : 'stopped' }}</dd></div><div><dt>Domains</dt><dd>{{ c.proxy.domains }}</dd></div><div><dt>HTTPS</dt><dd>{{ c.proxy.https_live ? 'port ' + c.proxy.https_port : 'off' }}</dd></div><div><dt>Let's Encrypt certificates</dt><dd>{{ c.proxy.certs }}</dd></div></dl></section>
      <p v-if="c.erp.installed && c.proxy.installed && c.proxy.frontend_local && !c.proxy.https_live" class="notice">{{ c.proxy.https_reason }}</p>
      <p v-if="c.erp.installed && c.proxy.installed && !c.proxy.frontend_local" class="notice">This stack was installed before nginx routing: its frontend still holds the public port. <b>Repair nginx routing</b> moves it.</p>
      <div v-if="c.erp.installed && s.scripts.proxy && !fine(c.proxy)"><button class="btn primary" :disabled="job.active" @click="go(c)">{{ c.proxy.installed ? 'Repair nginx routing' : 'Install nginx routing' }}</button></div>
      <p class="hint">Manage domains on each <RouterLink to="/sites">site's</RouterLink> Domains page. Each domain must still resolve to this server: a DNS A record (public DNS or the router) or a hosts-file line per PC. HTTPS works on every domain with a self-signed certificate (browsers warn); a public domain gets a trusted Let's Encrypt one with <b>Get certificate</b>.</p>
    </template>
  </SetupFrame>
</template>
