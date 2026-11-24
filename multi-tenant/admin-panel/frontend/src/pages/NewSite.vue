<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { sites } from '../data.js'
import { job, runAction } from '../useJob.js'
const router = useRouter()
const f = reactive({ host: '', pw: '', pw2: '', apps: [] }), msg = ref('')
const image = computed(() => sites.data?.image_apps || [])
let filled = false
watch(image, a => { if (!filled && a.length) { f.apps = [...a]; filled = true } }, { immediate: true })
async function create() {
  const host = f.host.trim().toLowerCase()
  if (!/^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$/.test(host)) { msg.value = 'Enter a lowercase hostname, e.g. acme.erp.lan'; return }
  if (!f.pw || f.pw !== f.pw2) { msg.value = 'Passwords are empty or do not match'; return }
  msg.value = ''
  const body = { site: host, admin_password: f.pw }
  if (image.value.length) body.apps = image.value.filter(a => f.apps.includes(a))
  if (await runAction('new_site', body, `Create site ${host}?\n\nApps: ${(body.apps || ['every app in the image']).join(', ')}\n\n5–10 minutes. Afterwards point ${host} at this server in DNS or the hosts file.`, { action: 'Create site' })) {
    f.pw = f.pw2 = ''
    router.push('/sites')
  }
}
</script>
<template>
  <div class="view">
    <PageHead title="Create site" back="/sites" back-label="sites" lead="Takes 5–10 minutes. Adding a hostname briefly restarts the backend and workers (a few seconds, all sites)." />
    <p v-if="sites.data && !(sites.data.stack && sites.data.tools)" class="notice">Install the ERPNext stack and tools first. <RouterLink to="/setup/stack">Open setup</RouterLink></p>
    <form v-else class="panel form" style="max-width:none" @submit.prevent="create">
      <label class="field"><span>Hostname</span><input v-model="f.host" id="ns-host" placeholder="acme.erp.lan" autocomplete="off" autocapitalize="off" spellcheck="false"><small>Afterwards make it resolve to this server: a DNS record (public DNS or the router) or a hosts-file line on each PC.</small></label>
      <label class="field"><span>Administrator password</span><input v-model="f.pw" id="ns-pw" type="password" autocomplete="new-password"></label>
      <label class="field"><span>Confirm password</span><input v-model="f.pw2" id="ns-pw2" type="password" autocomplete="new-password"></label>
      <div class="field"><span>Apps</span>
        <div v-if="image.length" class="checks"><label v-for="a in image" :key="a" class="check"><input v-model="f.apps" type="checkbox" :value="a">{{ a }}</label></div>
        <small v-else>Every app in the image (the list is not readable yet).</small>
        <small>Apps left unticked are recorded, so later updates never install them on this site.</small>
        <small v-if="image.length && !sites.data.apps_live" class="error">The backend is not answering. This list is read from apps.txt and may be out of date.</small></div>
      <p v-if="sites.data" class="hint">
        <template v-if="sites.data.cloud.length">Cloud backup: the new site's backups also upload to <template v-for="(x, i) in sites.data.cloud" :key="x">{{ i ? ' and ' : '' }}<code>{{ x }}/sites/&lt;site&gt;/</code></template> (the same destination as every site). <RouterLink to="/cloud">Change it</RouterLink></template>
        <template v-else>Cloud backup is not set up, so backups stay on this server. <RouterLink to="/cloud">Set it up</RouterLink> once and it covers every site, including new ones.</template></p>
      <p v-if="msg" class="error" role="alert">{{ msg }}</p>
      <button class="btn primary" :disabled="job.active">Create site</button>
    </form>
  </div>
</template>
