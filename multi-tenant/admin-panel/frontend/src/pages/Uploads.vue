<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { postJSON } from '../api.js'
import { enc, resource, upload, usePoll } from '../data.js'
import { ask, toast } from '../ui.js'
import { job } from '../useJob.js'
const b = resource('/api/backups'), router = useRouter()
usePoll(b.load, 60000)
const groups = computed(() => {
  const m = new Map()
  for (const f of b.data?.uploads || []) {
    const i = f.name.indexOf('/'), k = i < 0 ? '' : f.name.slice(0, i)
    if (!m.has(k)) m.set(k, [])
    m.get(k).push(f)
  }
  return [...m].sort((x, y) => (x[0] === '') - (y[0] === ''))
})
const restorable = k => (b.data.upload_folders || []).includes(k)
async function del(name, folder) {
  if (!await ask(folder ? `Delete the uploaded folder "${name}" and everything in it?\n\nThis cannot be undone.` : `Delete the uploaded file "${name}"?\n\nThis cannot be undone.`, { action: 'Delete', danger: true })) return
  try { const j = await postJSON('/api/uploads/delete', { name }); if (j.error) toast(j.error, 'error') } catch { toast('The request failed.', 'error') }
  b.load()
}
const filesIn = ref(null), dirIn = ref(null), folder = ref(''), sel = ref('')
const EXT = /\.(sql\.gz|gz|tar|tgz|json|ya?ml)$/i
function chosen() {
  const d = [...dirIn.value.files]
  return d.length ? d.filter(f => f.webkitRelativePath.split('/').length === 2 && EXT.test(f.name)) : [...filesIn.value.files]
}
function onDir() {
  const all = [...dirIn.value.files]
  if (!all.length) return
  filesIn.value.value = ''
  folder.value = all[0].webkitRelativePath.split('/')[0]
  const n = chosen().length
  sel.value = `${n} backup files selected; ${all.length - n} unsupported or nested files skipped.`
}
function onFiles() {
  if (!filesIn.value.files.length) return
  dirIn.value.value = ''
  sel.value = `${filesIn.value.files.length} files selected.`
}
const say = (t, e = false) => { up.text = t; up.bad = e }
async function send() {
  const files = chosen(), dest = folder.value.trim()
  if (!files.length) return say('Choose backup files or a folder containing supported backup files first.', true)
  if ((dirIn.value.files.length && !dest) || (dest && !/^[A-Za-z0-9_-]{1,40}$/.test(dest))) return say('Enter a destination folder of 1–40 letters, digits, hyphens or underscores.', true)
  const fd = new FormData()
  for (const f of files) fd.append('files', f, f.name)
  fd.append('folder', dest)
  up.busy = true
  const j = await upload('/upload', fd, say)
  up.busy = false
  if (j) say(`Uploaded to ${j.dest}: ${j.saved.join(', ')}`)
  b.load()
}
onMounted(() => { if (!up.busy) say('') })
</script>
<script>
import { reactive } from 'vue'
// Outside the page, so a running upload is still shown after leaving and coming back.
const up = reactive({ text: '', bad: false, busy: false })
</script>
<template>
  <div class="view">
    <PageHead title="Uploads" lead="Bring a backup to this server. Put one backup per folder; the folder then gets a Restore button that restores it into a site you choose." />
    <form class="panel form" style="max-width:none" @submit.prevent="send"><h2>Upload backup files</h2>
      <label class="field"><span>Files</span><input id="upfiles" ref="filesIn" type="file" multiple @change="onFiles"></label>
      <label class="field"><span>Or a whole backup folder</span><input id="updirectory" ref="dirIn" type="file" webkitdirectory multiple @change="onDir"><small>Supported files directly inside it are uploaded together; subfolders and other file types are skipped.</small></label>
      <label class="field" style="max-width:360px"><span>Destination folder</span><input id="upfolder" v-model="folder" placeholder="optional"><small>Allowed files: .sql.gz, .tar, .tgz, .json, .yml</small></label>
      <p v-if="sel" class="hint" aria-live="polite">{{ sel }}</p>
      <p v-if="up.text" :class="up.bad ? 'error' : 'good'" role="status">{{ up.text }}</p>
      <button class="btn primary" :disabled="up.busy">Upload</button></form>
    <p v-if="b.error" class="notice">{{ b.error }}</p>
    <section v-for="[k, files] in groups" :key="k" class="panel flush">
      <div class="panel-top"><h2>{{ k ? k + '/' : 'Files without a folder' }}</h2><div v-if="k" class="actions"><Badge v-if="!restorable(k)" kind="warn" title="Upload a matching *-database.sql.gz file into this folder first">database backup missing</Badge><button class="btn small danger" :disabled="job.active || !restorable(k)" @click="router.push({ path: '/restore', query: { kind: 'upload', src: '', name: k } })">Restore</button><button class="btn small danger" @click="del(k, true)">Delete folder</button></div></div>
      <div class="scroll tall"><table><thead><tr><th>File</th><th>Size</th><th>Date</th><th></th></tr></thead><tbody><tr v-for="f in files" :key="f.name"><td>{{ f.name }}</td><td>{{ f.size }}</td><td>{{ f.mtime }}</td><td><div class="actions"><a class="btn small" :href="'/download/upload/' + enc(f.name)">Download</a><button class="btn small danger" @click="del(f.name, false)">Delete</button></div></td></tr></tbody></table></div></section>
    <p v-if="b.data && !groups.length" class="hint">No uploads yet. Enter a folder name above to keep a backup's files together.</p>
  </div>
</template>
