<script setup>
import { ref } from 'vue'
import { getJSON } from '../api.js'
import { siteNames, upload } from '../data.js'
const file = ref(null), msg = ref(''), bad = ref(false), site = ref(''), files = ref([]), dmsg = ref(''), dbad = ref(false), shown = ref('')
const say = (t, e = false) => { msg.value = t; bad.value = e }
async function send() {
  const f = file.value.files[0]
  if (!f) return say('Choose a license file first.', true)
  const fd = new FormData()
  fd.append('file', f)
  const j = await upload('/upload-license', fd, say)
  if (j) say(`Uploaded ${j.saved} to ${j.site}`)
}
const href = n => `/download-license?site=${encodeURIComponent(shown.value)}&name=${encodeURIComponent(n)}`
async function find() {
  files.value = []; dbad.value = false
  if (!site.value) { dmsg.value = 'Choose a site first.'; dbad.value = true; return }
  dmsg.value = 'Looking for a license file…'
  try {
    const want = site.value, j = await getJSON('/api/licenses?site=' + encodeURIComponent(want))
    if (want !== site.value) return
    if (j.error || !j.files.length) { dmsg.value = j.error || `No license file in ${j.site}.`; dbad.value = true; return }
    shown.value = want; files.value = j.files; dmsg.value = ''
    if (j.files.length === 1) location.href = href(j.files[0])
  } catch { dmsg.value = 'Could not reach the server.'; dbad.value = true }
}
</script>
<template>
  <div class="view">
    <PageHead title="Licenses" lead="A license is stored in its site's folder in the Docker sites volume." />
    <form class="panel" @submit.prevent="send"><h2>Upload a license</h2><p class="hint">The file must contain a <code>"site"</code> field, which decides where it goes. Allowed: .lic, .txt, .json, .key.</p>
      <div class="inline"><input ref="file" type="file" aria-label="License file"><button class="btn primary">Upload license</button></div>
      <p v-if="msg" :class="bad ? 'error' : 'good'" role="status" style="margin-top:10px">{{ msg }}</p></form>
    <form class="panel" @submit.prevent="find"><h2 style="margin-bottom:14px">Download a license</h2>
      <div class="inline"><select v-model="site" aria-label="Site" @change="files = []; dmsg = ''"><option value="">Choose a site</option><option v-for="s in siteNames()" :key="s">{{ s }}</option></select><button class="btn">Download license</button></div>
      <p v-if="dmsg" :class="dbad ? 'error' : 'hint'" role="status" style="margin-top:10px">{{ dmsg }}</p>
      <div v-if="files.length" class="files" style="margin-top:12px"><a v-for="n in files" :key="n" :href="href(n)">{{ n }}</a></div></form>
  </div>
</template>
