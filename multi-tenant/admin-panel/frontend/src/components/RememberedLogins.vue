<script setup>
import { ref } from 'vue'
import { getJSON } from '../api.js'
import { enc, usePoll } from '../data.js'
import { job, runAction } from '../useJob.js'
const p = defineProps({ site: String }), j = ref(null)      // one instance per site (keyed)
let seq = 0
usePoll(async () => {
  const mine = ++seq
  let a
  try { a = await getJSON('/api/auth/' + enc(p.site) + '/sessions') } catch { a = { error: 'Could not load the remembered logins.' } }
  if (mine === seq) j.value = a
}, 60000)
const many = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`
const left = s => s >= 86400 ? many(Math.floor(s / 86400), 'day') : s >= 3600 ? many(Math.floor(s / 3600), 'hour') : 'under 1 hour'
const revoke = x => runAction('auth_session_revoke', { site: p.site, id: x.id },
  `Revoke this login of ${x.user} on ${p.site}?\n\nLogged in ${x.login}${x.ip ? ' from ' + x.ip : ''}. That browser must log in to Authelia again. The user's other browsers stay logged in.`,
  { action: 'Revoke', danger: true })
</script>
<template>
  <section class="panel flush"><div class="panel-top"><div><h2>Remembered logins</h2><p class="hint">Browsers that ticked Remember me. Until it expires, they enter this site without logging in to Authelia again. The address is where the user logged in.</p></div></div>
    <p v-if="!j" class="hint" style="padding:0 22px 16px">Loading…</p><p v-else-if="j.error" class="notice crit" style="margin:0 22px 16px">{{ j.error }}</p>
    <template v-else><p v-if="j.unreadable" class="hint" style="padding:0 22px 12px">{{ many(j.unreadable, 'session') }} could not be read and may be missing here.</p><div class="scroll"><table><thead><tr><th>User</th><th>IP</th><th>Logged in</th><th>Remaining</th><th></th></tr></thead><tbody>
      <tr v-for="x in j.sessions" :key="x.id"><td><b>{{ x.user }}</b> <Badge v-if="!x.twofa" kind="warn">password only</Badge><div class="sub">{{ x.host }}</div></td><td><code>{{ x.ip || '–' }}</code></td><td>{{ x.login }}</td><td><b>{{ left(x.left) }}</b><div class="sub">until {{ x.expires }}</div></td><td><div class="actions"><button class="btn small danger" :disabled="job.active" @click="revoke(x)">Revoke</button></div></td></tr>
      <tr v-if="!j.sessions.length"><td colspan="5" class="hint">No remembered logins.</td></tr></tbody></table></div></template></section>
</template>
