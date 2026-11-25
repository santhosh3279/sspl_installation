<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { getJSON, postJSON } from '../api.js'
import { ask, toast } from '../ui.js'
import { usePoll, wg } from '../data.js'
usePoll(wg.load, 15000)
const peer = useRoute().params.peer, https = location.protocol === 'https:'
const user = ref(''), busy = ref(false), state = ref('idle'), box = ref(null)   // state: idle, open, ended
let term, fit, watcher, id = '', pending = '', sending = false, timer
const base = () => '/api/wireguard/ssh/' + id
const post = (what, body) => postJSON(`${base()}/${what}`, body)

function teardown() { watcher?.disconnect(); clearTimeout(timer); term?.dispose(); term = fit = watcher = null }
async function open() {
  if (busy.value) return
  busy.value = true
  teardown()
  state.value = 'open'
  await nextTick()
  term = new Terminal({ cursorBlink: true, fontSize: 14, fontFamily: 'ui-monospace, "Cascadia Mono", Menlo, Consolas, monospace', scrollback: 5000, theme: { background: '#081c21', foreground: '#d4e4e5' } })
  fit = new FitAddon()
  term.loadAddon(fit)
  term.open(box.value)
  fit.fit()
  let error = ''
  try {
    const j = await postJSON('/api/wireguard/ssh', { name: peer, user: user.value.trim(), rows: term.rows, cols: term.cols })
    if (j.error) error = j.error
    else id = j.id
  } catch { error = 'The request failed. Check the connection and try again.' }
  busy.value = false
  if (error) { state.value = 'idle'; teardown(); return toast(error, 'error') }
  if (gone) return close()
  term.onData(d => { pending += d; flush() })
  term.onResize(({ rows, cols }) => { clearTimeout(timer); timer = setTimeout(() => { if (state.value === 'open') post('resize', { rows, cols }).catch(() => {}) }, 150) })
  watcher = new ResizeObserver(() => fit?.fit())
  watcher.observe(box.value)
  term.focus()
  read(id)
}
// One request at a time, so keys arrive in order; keys typed meanwhile go together.
async function flush() {
  if (sending) return
  sending = true
  while (pending && state.value === 'open') {
    let n = Math.min(pending.length, 16000)
    const c = pending.charCodeAt(n - 1)
    if (n < pending.length && c >= 0xd800 && c < 0xdc00) n--     // keep a surrogate pair together
    const data = pending.slice(0, n)
    pending = pending.slice(n)
    try {
      const j = await post('input', { data })
      if (j.error) { pending = ''; if (state.value === 'open') toast(j.error, 'error') }
    } catch { pending = ''; if (state.value === 'open') toast('Could not send the input.', 'error') }
  }
  pending = ''
  sending = false
}
async function read(mine) {
  let pos = 0, fails = 0
  while (mine === id && state.value === 'open') {
    try {
      const j = await getJSON(`${base()}/output?pos=${pos}`)
      if (mine !== id || !term) return
      fails = 0
      pos = j.pos
      if (j.data) term.write(Uint8Array.from(atob(j.data), c => c.charCodeAt(0)))
      if (j.done) break
    } catch (e) {
      if (String(e.message).endsWith(': 404') || ++fails > 5) break
      await new Promise(ok => setTimeout(ok, 1000))
    }
  }
  if (mine === id && state.value === 'open') end()
}
function end() { state.value = 'ended'; id = ''; pending = ''; if (term) { term.options.disableStdin = true; term.options.cursorBlink = false } }
function close() {
  if (id) fetch(`${base()}/close`, { method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: '{}' }).catch(() => {})
  end()
}
let gone = false
onMounted(() => addEventListener('pagehide', close))
onBeforeRouteLeave(async () => state.value !== 'open' || !!await ask('Leave and close this SSH session?', { action: 'Close session' }))
onBeforeUnmount(() => { gone = true; removeEventListener('pagehide', close); close(); teardown() })
</script>
<template>
  <div class="view">
    <PageHead :title="'SSH: ' + peer" lead="A terminal on this print server, through the WireGuard tunnel. The session closes when you leave the page or after 30 minutes without typing."><button v-if="state === 'open'" class="btn" :disabled="busy" @click="close">Disconnect</button><RouterLink class="btn" to="/printers">Print servers</RouterLink></PageHead>
    <p v-if="!https" class="notice crit">This page is not on HTTPS: the password would cross the network unencrypted.</p>
    <p v-if="wg.data && !wg.data.peers.some(p => p.name === peer && p.up)" class="notice">{{ peer }} is not connected.</p>
    <form v-if="state !== 'open'" class="panel" @submit.prevent="open"><p class="hint">{{ state === 'ended' ? 'The session has ended.' : 'The password is asked in the terminal and is not stored. Debian and Ubuntu usually refuse root with a password: use a normal user, then sudo.' }}</p><div class="inline"><label class="field"><span>User on {{ peer }}</span><input id="ssh-user" v-model="user" required maxlength="32" autocomplete="off" autocapitalize="off" spellcheck="false"></label><button class="btn primary" :disabled="busy || !user.trim()">{{ state === 'ended' ? 'Connect again' : 'Connect' }}</button></div></form>
    <div v-if="state !== 'idle'" class="ssh"><div ref="box"></div></div>
  </div>
</template>
