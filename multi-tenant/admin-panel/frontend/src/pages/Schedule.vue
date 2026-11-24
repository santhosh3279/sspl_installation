<script setup>
import { computed } from 'vue'
import { resource, setup, usePoll } from '../data.js'
import { job, runAction } from '../useJob.js'
const cron = resource('/api/cron')
usePoll(cron.load, 60000); usePoll(setup.load, 30000)
const WORDS = { '0 2 * * *': 'daily at 02:00', '0 */6 * * *': 'every 6 hours', '0 3 * * 0': 'Sundays at 03:00' }
const c = computed(() => setup.data?.components?.cron || setup.data?.cron || {}), toolsOk = computed(() => setup.data?.components?.tools?.installed)
const plan = () => runAction('install_tools', { schedule_cron: true }, "Schedule backups for every site?\n\nFull backup daily at 02:00, DB dump every 6 h, verify Sundays 03:00. Other lines in root's crontab are left alone. (Also refreshes the tools from the checkout.)", { action: 'Schedule backups' })
</script>
<template>
  <div class="view">
    <PageHead title="Schedule" lead="When backups run by themselves." />
    <p v-if="setup.error || cron.error" class="notice">{{ setup.error || cron.error }}</p>
    <template v-if="setup.data">
      <section v-if="!setup.data.repo_ok" class="notice">Installer scripts are not available, so the backup schedule cannot be checked. <RouterLink to="/setup/tools">Open setup</RouterLink></section>
      <section v-else-if="c.scheduled" class="panel flush"><div class="panel-top"><h2>Backups for all sites</h2><Badge kind="ok">scheduled</Badge></div><div class="scroll"><table><tbody><tr v-for="j in c.jobs" :key="j.command"><td><b>{{ WORDS[j.schedule] || j.schedule }}</b> <div class="sub">{{ j.schedule }}</div></td><td><code>{{ j.command }}</code></td></tr></tbody></table></div></section>
      <section v-else class="panel"><h2>Nothing is scheduled</h2><p class="hint">Full backup daily at 02:00, database dump every 6 hours, verification on Sundays at 03:00.</p><button class="btn primary" :disabled="job.active || !toolsOk" @click="plan">Schedule backups</button><p v-if="!toolsOk" class="hint" style="margin-top:10px">Install the stack first.</p></section>
      <section v-if="c.other?.length" class="notice crit"><b>Other backup-like jobs.</b> These are outside the multi-tenant block. Leftovers from a single-site setup would back up a site that is not here and fail nightly. Check them over SSH (<code>sudo crontab -e</code>).<div v-for="j in c.other" :key="j.command"><code>{{ j.schedule }} {{ j.command }}</code></div></section>
    </template>
    <section class="panel flush"><div class="panel-top"><h2>Root's crontab</h2><span class="hint">Same as <code>sudo crontab -l</code></span></div><div class="scroll"><table><thead><tr><th>Schedule</th><th>Command</th></tr></thead><tbody><tr v-for="(j, i) in cron.data?.jobs" :key="i"><td><code>{{ j.schedule }}</code></td><td><code>{{ j.command }}</code></td></tr><tr v-if="!cron.data?.jobs?.length"><td colspan="2" class="hint">{{ cron.data ? 'The crontab is empty: nothing is scheduled.' : 'Loading…' }}</td></tr></tbody></table></div></section>
  </div>
</template>
