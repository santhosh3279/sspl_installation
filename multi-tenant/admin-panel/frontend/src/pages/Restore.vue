<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { siteNames, sites } from '../data.js'
import { job, startAction } from '../useJob.js'
const { kind, src = '', name } = useRoute().query, router = useRouter()
const valid = ['full', 'upload'].includes(kind) && name
const f = reactive({ target: '', other: false, confirm: '', pw: '' }), error = ref(''), busy = ref(false)
watch(() => siteNames().join(), () => { const n = siteNames(); if (!n.includes(f.target)) f.target = n.includes(src) ? src : n[0] || '' }, { immediate: true })
// A site's own full backup never needs the cross-site switch.
const own = computed(() => kind === 'full' && src === f.target)
watch(own, o => { if (o) f.other = false }, { immediate: true })
watch(() => f.target, () => { f.confirm = '' })
async function go() {
  busy.value = true
  error.value = await startAction('restore', { kind, src_site: src, name, site: f.target, allow_other_site: f.other, confirm_site: f.confirm, admin_password: f.pw })
  busy.value = false
  if (!error.value) router.push('/sites/' + f.target)
}
</script>
<template>
  <div class="view">
    <PageHead title="Restore a site" :back="kind === 'upload' ? '/uploads' : '/backups'" :back-label="kind === 'upload' ? 'uploads' : 'backups'" />
    <p v-if="!valid" class="notice">Choose a backup to restore from the <RouterLink to="/backups">backups</RouterLink> or <RouterLink to="/uploads">uploads</RouterLink> page.</p>
    <p v-else-if="sites.data && !siteNames().length" class="notice">There is no site to restore into. <RouterLink to="/new-site">Create one first</RouterLink></p>
    <form v-else class="panel edge-danger form" style="max-width:none" @submit.prevent="go">
      <p class="notice crit"><b>This overwrites one live site.</b> Everything in <b>{{ f.target }}</b>, every record entered since the backup was taken, is replaced by <b>{{ kind === 'full' ? `${src} / ${name}` : `uploads/${name}` }}</b>.</p>
      <p class="hint">A <b>full safety backup of that site is taken first</b>. Only that site goes into maintenance mode; the other sites stay up, though their background jobs pause for the few minutes it takes.</p>
      <label class="field" style="max-width:420px"><span>Restore into site</span><select id="rs-target" v-model="f.target"><option v-for="s in siteNames()" :key="s">{{ s }}</option></select></label>
      <label class="check"><input id="rs-other" v-model="f.other" type="checkbox" :disabled="own"><span>The backup is from another site (moving a tenant). Its encryption key is copied into the target too, so encrypted fields stay readable.</span></label>
      <label class="field" style="max-width:420px"><span>Type the site name <b>{{ f.target }}</b> to confirm</span><input id="rs-confirm" v-model="f.confirm" autocomplete="off"></label>
      <label class="field" style="max-width:420px"><span>Your admin panel password</span><input id="rs-pw" v-model="f.pw" type="password" autocomplete="current-password"></label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button class="btn danger-solid" :disabled="job.active || busy || !f.target">Back up, then restore</button>
    </form>
  </div>
</template>
