<script setup>
import { backupTime, sites } from '../data.js'
import { job, runAction } from '../useJob.js'
</script>
<template>
  <div class="view">
    <PageHead title="Sites" lead="One stack serves every site. nginx picks the site from the hostname in the browser's address bar. Per-site jobs touch only that site; the others stay up.">
      <RouterLink v-if="sites.data?.stack && sites.data?.tools" class="btn primary" to="/new-site">Create site</RouterLink>
    </PageHead>
    <p v-if="sites.error" class="notice">{{ sites.error }}</p>
    <p v-if="sites.data && !sites.data.stack" class="notice">No ERPNext stack on this server yet. <RouterLink to="/setup/stack">Install it</RouterLink></p>
    <section class="panel flush"><div class="scroll"><table>
      <thead><tr><th>Site</th><th>State</th><th>Last full backup</th><th></th></tr></thead>
      <tbody>
        <tr v-for="s in sites.data?.sites" :key="s.name">
          <td><RouterLink :to="'/sites/' + s.name"><b>{{ s.name }}</b></RouterLink>
            <div v-if="s.domain_info.length > 1" class="sub">also <template v-for="(d, i) in s.domain_info.slice(1)" :key="d.name">{{ i ? ', ' : '' }}<a :href="d.url" target="_blank" rel="noopener">{{ d.name }}</a></template></div>
            <div v-if="s.skip_apps.length" class="sub">never auto-installed: {{ s.skip_apps.join(', ') }}</div></td>
          <td><div class="checks"><Badge :kind="s.maintenance ? 'warn' : 'ok'">{{ s.maintenance ? 'maintenance' : 'online' }}</Badge><Badge v-if="s.auth" kind="ok">login required</Badge></div></td>
          <td><template v-if="s.latest_backup">{{ backupTime(s.latest_backup) }} <span class="sub">({{ s.backups }} kept)</span></template><Badge v-else kind="warn">none</Badge></td>
          <td><div class="actions"><button class="btn small" :disabled="job.active" @click="runAction('backup', { site: s.name })">Back up</button><a class="btn small" :href="s.url" target="_blank" rel="noopener">Open</a></div></td>
        </tr>
        <tr v-if="!sites.data?.sites.length"><td colspan="4" class="hint">{{ sites.data ? 'No sites yet. Create the first one.' : 'Loading…' }}</td></tr>
      </tbody>
    </table></div></section>
  </div>
</template>
