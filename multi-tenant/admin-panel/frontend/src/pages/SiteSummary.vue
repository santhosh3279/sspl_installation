<script setup>
import SiteFrame from '../components/SiteFrame.vue'
import { backupTime } from '../data.js'
import { job, runAction } from '../useJob.js'
</script>
<template>
  <SiteFrame v-slot="{ s }">
    <section class="panel"><dl class="facts">
      <div><dt>Address</dt><dd><a :href="s.url" target="_blank" rel="noopener">{{ s.url }}</a></dd></div>
      <div><dt>Last full backup</dt><dd>{{ s.latest_backup ? backupTime(s.latest_backup) : 'none' }}<small>{{ s.backups }} kept on the server</small></dd></div>
      <div><dt>Addresses</dt><dd>{{ s.domain_info.length }}</dd></div>
      <div><dt>Scheduler</dt><dd>{{ s.paused ? 'paused' : 'on' }}</dd></div>
    </dl></section>
    <section class="panel"><h2>Run on this site</h2><p class="hint">Only this site is affected. The others stay up.</p>
      <div class="actions">
        <button class="btn primary" :disabled="job.active" @click="runAction('backup', { site: s.name })">Full backup</button>
        <button class="btn" :disabled="job.active" @click="runAction('db_backup', { site: s.name })">Database backup</button>
        <button class="btn" :disabled="job.active" @click="runAction('migrate', { site: s.name }, `Run database migrations on ${s.name}?`, { action: 'Migrate' })">Migrate</button>
      </div></section>
    <p v-if="s.skip_apps.length" class="hint">Never auto-installed here: {{ s.skip_apps.join(', ') }}</p>
  </SiteFrame>
</template>
