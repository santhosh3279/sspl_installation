<script setup>
import { ref } from 'vue'
import { api } from '../api.js'
import { addNet } from '../access.js'
import { usePoll } from '../data.js'
import { job } from '../useJob.js'
const p = defineProps({ site: String }), j = ref(null)      // one instance per site (keyed)
usePoll(async () => {
  try { j.value = await (await api('/api/auth/' + encodeURIComponent(p.site) + '/logins')).json() } catch { j.value = { error: 'Could not load the login log.' } }
}, 60000)
const v4 = ip => /^\d+\.\d+\.\d+\.\d+$/.test(ip)
</script>
<template>
  <p v-if="!j" class="hint">Loading…</p><p v-else-if="j.error" class="notice crit">{{ j.error }}</p>
  <template v-else>
    <section class="panel flush"><div class="panel-top"><h2>Where each user entered 2FA</h2></div><div class="scroll"><table><thead><tr><th>User</th><th>IP</th><th>Method</th><th>Last success</th><th>Passed / failed</th><th></th></tr></thead><tbody><tr v-for="x in j.twofa" :key="x.user + x.ip + x.type"><td><b>{{ x.user }}</b></td><td><code>{{ x.ip || '–' }}</code></td><td>{{ x.type }}</td><td>{{ x.last || 'never' }}</td><td>{{ x.good }} / <span :class="{ error: x.bad }">{{ x.bad }}</span></td><td><div v-if="x.good && v4(x.ip)" class="actions"><button class="btn small" :disabled="job.active" @click="addNet(site, 'bypass', x.ip)">Whitelist</button><button class="btn small" :disabled="job.active" @click="addNet(site, 'trusted', x.ip)">No 2FA</button></div></td></tr><tr v-if="!j.twofa.length"><td colspan="6" class="hint">No 2FA logins recorded.</td></tr></tbody></table></div></section>
    <section class="panel flush"><div class="panel-top"><h2>Last 100 login attempts</h2></div><div class="scroll tall"><table><thead><tr><th>Time</th><th>User</th><th>IP</th><th>Step</th><th>Result</th></tr></thead><tbody><tr v-for="(x, i) in j.recent" :key="i"><td>{{ x.time }}</td><td>{{ x.user }}</td><td><code>{{ x.ip || '–' }}</code></td><td>{{ x.type }}</td><td><Badge :kind="x.banned || !x.ok ? 'crit' : 'ok'">{{ x.banned ? 'banned' : x.ok ? 'ok' : 'failed' }}</Badge></td></tr><tr v-if="!j.recent.length"><td colspan="5" class="hint">No logins recorded.</td></tr></tbody></table></div></section>
  </template>
</template>
