<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { postJSON } from '../api.js'
import { setup, usePoll } from '../data.js'
import { ask } from '../ui.js'
import { job, runAction } from '../useJob.js'
usePoll(setup.load, 30000)
const s = computed(() => setup.data), r = computed(() => s.value?.components?.rclone || {})
const targets = computed(() => r.value.wired_targets || [])
const shown = computed(() => r.value.wired_all && targets.value.length ? r.value.wired + ' → ' + targets.value.join(' ') : r.value.wired || '')
const bad = computed(() => targets.value.filter(t => !(r.value.remotes || []).some(x => t.startsWith(x))))
const https = location.protocol === 'https:'
// Seeded once from the server, then left to the user until saved.
const w = reactive({ remotes: [], all: false, path: '' }), seeded = ref(false), msg = ref('')
watch(r, x => {
  if (seeded.value || !x.configured) return
  const first = (x.wired || '').split(/\s+/)[0] || ''
  w.remotes = x.wired_all ? [] : (x.remotes || []).filter(m => (x.wired_targets || []).some(t => t.startsWith(m)))
  w.all = !!x.wired_all
  w.path = first ? first.replace(/^[^:]*:/, '') : 'frappe-backups'
  seeded.value = true
}, { immediate: true })
async function wire(body) {
  msg.value = 'Saving…'
  try {
    const j = await postJSON('/api/rclone/wire', body)
    msg.value = j.error || (j.wired ? 'Saved. Backups now upload to ' + j.wired : 'Saved. Backups stay on this server only.')
    if (!j.error) { seeded.value = false; setup.load() }
  } catch { msg.value = 'The request failed.' }
}
async function save() {
  const remotes = (r.value.remotes || []).filter(x => w.remotes.includes(x))
  if (!w.all && !remotes.length) { msg.value = 'Tick at least one remote'; return }
  if (w.all && !await ask("Upload every site's backups to EVERY remote rclone knows, now and in future?\n\nBackups contain each site's site_config.json: its database password and encryption key.", { action: 'Upload to every remote' })) return
  wire({ remotes, all: w.all, path: w.path.trim() })
}
async function off() {
  if (await ask('Stop uploading backups to the cloud?\n\nBackups already uploaded stay where they are.', { action: 'Stop uploading', danger: true })) wire({ off: true })
}
const acc = reactive({ name: 'gdrive', token: '', busy: false, msg: '' })
async function connect() {
  const name = acc.name.trim(), token = acc.token.trim()
  if (!token) { acc.msg = 'Paste the token first.'; return }
  if (r.value.wired_all && !await ask("Backups go to EVERY remote rclone knows, so this account will receive every site's backups. Continue?", { action: 'Connect' })) return
  const post = replace => postJSON('/api/rclone/connect', { name, token, replace })
  acc.busy = true; acc.msg = 'Testing the account…'
  try {
    let j = await post(false)
    if (j.exists) {
      if (!await ask(`Reconnect ${name} with this account?\n\nIf it is a different Google account, backups go there from now on.`, { action: 'Reconnect' })) { acc.msg = ''; return }
      j = await post(true)
    }
    if (j.error) { acc.msg = j.error; return }
    acc.token = ''
    await setup.load()
    acc.msg = 'Connected ' + j.remote + ' Now choose where backups upload.'
  } catch { acc.msg = 'The request failed.' } finally { acc.busy = false }
}
const install = () => runAction('install_rclone', {}, 'Install rclone on this server?\n\nIt downloads the official rclone binary.', { action: 'Install rclone' })
</script>
<template>
  <div class="view">
    <PageHead title="Cloud storage" lead="Copy every site's backups off this server. All three steps must be done before a backup leaves it; a failed upload does not fail the backup."><Badge v-if="s" :kind="r.ready ? 'ok' : 'warn'">{{ r.ready ? 'uploading' : 'not set up' }}</Badge></PageHead>
    <p v-if="setup.error" class="notice">{{ setup.error }}</p>
    <p v-if="s && !s.repo_ok" class="notice">Installer scripts are not available. <RouterLink to="/setup/tools">Open setup</RouterLink></p>
    <template v-else-if="s">
      <section class="panel"><div class="panel-top"><h2>1. Install rclone</h2><Badge :kind="r.installed ? 'ok' : 'warn'">{{ r.installed ? 'installed' : 'not installed' }}</Badge></div>
        <p class="hint">{{ r.installed ? r.version : 'rclone is the program that uploads the backups.' }}</p>
        <button v-if="!r.installed && s.scripts.rclone" class="btn primary" style="margin-top:12px" :disabled="job.active" @click="install">Install rclone</button></section>
      <section class="panel"><div class="panel-top"><h2>2. Connect an account</h2><Badge :kind="r.configured ? 'ok' : 'warn'">{{ r.configured ? (r.remotes || []).join(' ') : 'no remote configured' }}</Badge></div>
        <form v-if="r.installed" class="form" style="margin-top:12px" @submit.prevent="connect">
          <p class="hint">On your own computer, install rclone and run <code>rclone authorize "drive"</code>, sign in to Google, then paste what it prints. The token gives access to that whole Drive: it is tested, then stored only in root's rclone config.</p>
          <p v-if="!https" class="notice crit">This page is not on HTTPS: the token would cross the network unencrypted. Use an SSH tunnel or VPN.</p>
          <label class="field"><span>Remote name</span><input id="rc-name" v-model="acc.name" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
          <label class="field"><span>Google Drive token</span><input id="rc-token" v-model="acc.token" type="password" autocomplete="off" placeholder="paste the token from rclone authorize"></label>
          <p v-if="acc.msg" class="hint" role="status">{{ acc.msg }}</p>
          <button class="btn" :class="{ primary: !r.configured }" :disabled="acc.busy">{{ r.configured ? 'Connect or reconnect Google Drive' : 'Connect Google Drive' }}</button></form></section>
      <section class="panel"><div class="panel-top"><h2>3. Choose where backups upload</h2><Badge :kind="r.wired_ok ? 'ok' : 'warn'">{{ shown || (r.wired_ok ? 'on' : 'backups stay local only') }}</Badge></div>
        <p v-if="r.wired && !r.wired_ok" class="notice crit" style="margin-top:12px">RCLONE_REMOTE points at <code>{{ bad.length ? bad.join(' ') : r.wired }}</code>, but rclone has no such remote{{ r.installed ? '' : ' (rclone is not installed)' }}: {{ bad.length && bad.length < targets.length ? 'that copy is silently not being made' : 'uploads are failing silently' }}. Pick your remotes below.</p>
        <p v-if="!r.conf_ok" class="hint">Install the ERPNext stack first. The destination is stored in <code>{{ s.mt_conf }}</code>.</p>
        <form v-else-if="r.configured" class="form" style="margin-top:12px" @submit.prevent="save">
          <div class="checks"><label v-for="x in r.remotes" :key="x" class="check"><input v-model="w.remotes" type="checkbox" :value="x" :disabled="w.all">{{ x }}</label></div>
          <label class="check"><input id="rc-all" v-model="w.all" type="checkbox"><span>Every remote rclone knows, including any added later</span></label>
          <label class="field"><span>Folder</span><input id="rc-path" v-model="w.path"><small>Each site uploads to &lt;remote&gt;&lt;folder&gt;/sites/&lt;site&gt;/</small></label>
          <p class="hint">Backups contain every site's <code>site_config.json</code>: its database password and encryption key. Every remote you tick receives them; "every remote rclone knows" also hands them to remotes you add in future, without asking again.</p>
          <p v-if="msg" class="hint" role="status">{{ msg }}</p>
          <div class="actions"><button id="rc-wire" class="btn primary">{{ r.wired ? 'Update destinations' : 'Upload backups here' }}</button><button v-if="r.wired" type="button" class="btn danger" @click="off">Stop uploading</button></div></form></section>
      <section class="panel"><h2>Connecting a Google account</h2><ol class="steps">
        <li>On your own computer (it needs a browser), install a recent rclone from <a href="https://rclone.org/downloads/" target="_blank" rel="noopener">rclone.org/downloads</a>.</li>
        <li>Run <code>rclone authorize "drive"</code>, sign in to Google and allow access.</li>
        <li>Copy the result, from <code>{</code> to <code>}</code>, and paste it at step 2. Press <b>Connect Google Drive</b>: the account is tested before anything is saved.</li>
        <li>The remote appears at step 2, and step 3 points every site's backups at it. New sites are included automatically.</li>
        <li>If uploads start failing with an expired or revoked token, do the same again with the same name.</li></ol>
        <p class="hint" style="margin-top:12px">Other clouds (MEGA, S3, Dropbox), your own Google client ID, or a password-protected rclone config: use <code>sudo rclone config</code> over SSH. With <b>sudo</b>, because backups run as root.</p>
        <p v-if="s.guide_ok" style="margin-top:10px"><a href="/guide/rclone">Download the full guide (.docx)</a> <span class="hint">Also covers AWS S3, Dropbox, testing a remote, and encrypting the rclone config.</span></p></section>
    </template>
  </div>
</template>
