<script setup>
import { computed, ref } from 'vue'
import BackupLists from '../components/BackupLists.vue'
import { postJSON } from '../api.js'
import { resource, usePoll } from '../data.js'
import { ask } from '../ui.js'
import { job, runAction } from '../useJob.js'
const b = resource('/api/backups'), want = ref(''), msg = ref('')
const load = () => b.load('/api/backups' + (want.value ? '?site=' + encodeURIComponent(want.value) : ''))
usePoll(load, 60000)
const cur = computed(() => b.data?.site || '')
const gone = computed(() => cur.value && !b.data.live.includes(cur.value))
function pick(e) { want.value = e.target.value; load() }
async function clearOld() {
  if (!await ask('Delete full and DB-only backups older than 30 days, for every site?\n\nThe newest 20 of each, per site, are always kept, however old. Uploads and image snapshots are not touched.', { action: 'Delete old backups', danger: true })) return
  msg.value = 'Clearing…'
  try {
    const j = await postJSON('/api/backups/clear-old', {})
    msg.value = j.error || (j.deleted ? `Deleted ${j.deleted} old backup${j.deleted === 1 ? '' : 's'}. Freed ${j.freed}.` : 'Nothing older than 30 days to delete.')
  } catch { msg.value = 'The request failed.' }
  load()
}
</script>
<template>
  <div class="view">
    <PageHead title="Backups" lead="Backups kept on this server, per site." />
    <section class="panel"><h2>Every site</h2><p class="hint">These run on all sites, one after another.</p>
      <div class="actions"><button class="btn primary" :disabled="job.active" @click="runAction('backup', { all: true })">Full backup of all sites</button><button class="btn" :disabled="job.active" @click="runAction('db_backup', { all: true })">Database backup of all</button><button class="btn" :disabled="job.active" @click="runAction('verify', { all: true })">Verify all backups</button></div></section>
    <p v-if="b.error" class="notice">{{ b.error }}</p>
    <div class="inline"><label class="field"><span>Site</span><select id="bk-site" :value="cur" @change="pick"><option v-if="!b.data?.sites.length" value="">no backups yet</option><option v-for="x in b.data?.sites" :key="x" :value="x">{{ x }}{{ b.data.live.includes(x) ? '' : ' (dropped)' }}</option></select></label></div>
    <p v-if="gone" class="notice">This site is no longer on the bench. Its backups are kept until you delete them over SSH.</p>
    <BackupLists :data="b.data" :site="cur" />
    <section class="panel"><h2>Clear old backups</h2><p class="hint">Frees disk space. The newest 20 of each kind are always kept for every site.</p><div class="actions"><button class="btn danger" @click="clearOld">Clear backups older than 30 days</button><span class="hint" role="status">{{ msg }}</span></div></section>
  </div>
</template>
