<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { enc } from '../data.js'
import { job } from '../useJob.js'
const p = defineProps({ data: Object, site: String })
const ok = computed(() => p.data && p.data.site === p.site), router = useRouter()
const full = computed(() => ok.value ? p.data.full : []), db = computed(() => ok.value ? p.data.db_only : [])
const restore = d => router.push({ path: '/restore', query: { kind: 'full', src: p.site, name: d.name } })
</script>
<template>
  <section class="panel flush"><div class="panel-top"><h2>Full backups</h2><span class="hint">{{ full.length }}</span></div>
    <div class="scroll tall"><table><thead><tr><th>Backup</th><th>Size</th><th>Contents</th><th>Files</th><th></th></tr></thead><tbody>
      <tr v-for="d in full" :key="d.name"><td>{{ d.name }}</td><td>{{ d.size }}</td>
        <td><div class="checks"><Badge v-for="[k, l] in [['db', 'Database'], ['public', 'Files'], ['private', 'Private files']]" :key="k" :kind="d[k] ? 'ok' : 'crit'">{{ l }}{{ d[k] ? '' : ' missing' }}</Badge></div></td>
        <td><details><summary>{{ d.files.length }} {{ d.files.length === 1 ? "file" : "files" }}</summary><div class="files"><a v-for="f in d.files" :key="f.name" :href="'/download/full/' + enc(site + '/' + d.name + '/' + f.name)">{{ f.name }} ({{ f.size }})</a></div></details></td>
        <td><div class="actions"><a class="btn small" :href="`/download-folder/full/${encodeURIComponent(site)}/${encodeURIComponent(d.name)}`" title="Download this whole backup folder as one .zip">Download</a><button v-if="d.db" class="btn small danger" data-restore :disabled="job.active" @click="restore(d)">Restore</button></div></td></tr>
      <tr v-if="!full.length"><td colspan="5" class="hint">{{ data ? 'No full backups.' : 'Loading…' }}</td></tr></tbody></table></div></section>
  <section class="panel flush"><div class="panel-top"><h2>Database-only backups</h2><span class="hint">{{ db.length }}</span></div>
    <div class="scroll tall"><table><thead><tr><th>File</th><th>Size</th><th>Date</th><th></th></tr></thead><tbody>
      <tr v-for="f in db" :key="f.name"><td>{{ f.name }} <Badge v-if="f.latest" kind="ok">latest</Badge></td><td>{{ f.size }}</td><td>{{ f.mtime }}</td><td class="r"><a class="btn small" :href="'/download/db/' + enc(site + '/' + f.name)">Download</a></td></tr>
      <tr v-if="!db.length"><td colspan="4" class="hint">{{ data ? 'No database-only backups.' : 'Loading…' }}</td></tr></tbody></table></div></section>
</template>
