<script setup>
import { computed } from 'vue'
import { backupTime, sites, stats, usePoll } from '../data.js'
usePoll(stats.load, 5000)
const s = computed(() => stats.data), list = computed(() => sites.data?.sites || [])
const alerts = computed(() => {
  const out = [], d = sites.data
  if (d && !d.stack) out.push(['crit', 'No ERPNext stack on this server yet.', '/setup/stack', 'Install it'])
  for (const x of list.value) {
    if (x.maintenance) out.push(['warn', `${x.name} is in maintenance mode: users see the maintenance page.`, '/sites/' + x.name, 'Open site'])
    if (!x.latest_backup) out.push(['warn', `${x.name} has no full backup.`, '/sites/' + x.name, 'Back it up'])
  }
  if (d?.stack && !d.cloud.length) out.push(['warn', 'Cloud backup is not set up. Backups stay on this server.', '/cloud', 'Set it up'])
  if (s.value) {
    for (const c of s.value.containers.services) if (c.state !== 'running') out.push(['crit', `Container ${c.name} is ${c.state}.`, '/containers', 'Containers'])
    if (s.value.containers.error) out.push(['crit', s.value.containers.error, '/containers', 'Containers'])
    for (const x of s.value.disks) if (x.pct >= 80) out.push([x.pct >= 92 ? 'crit' : 'warn', `${x.label} disk is ${x.pct}% full.`, '/backups', 'Backups'])
    if (s.value.mem.pct >= 80) out.push([s.value.mem.pct >= 92 ? 'crit' : 'warn', `Memory is ${s.value.mem.pct}% used.`, '/memory', 'Memory'])
  }
  return out
})
</script>
<template>
  <div class="view">
    <PageHead title="Overview"><RouterLink class="btn primary" to="/new-site">Create site</RouterLink></PageHead>
    <p v-if="sites.error || stats.error" class="notice" role="status">{{ sites.error || stats.error }}</p>
    <div v-if="sites.data && s" class="stack-list">
      <div v-for="([kind, text, to, label], i) in alerts" :key="i" class="notice" :class="kind">{{ text }} <RouterLink :to="to">{{ label }}</RouterLink></div>
      <div v-if="!alerts.length" class="notice ok">Nothing needs attention: every site is online and backed up, and every container is running.</div>
    </div>
    <section class="panel flush">
      <div class="panel-top"><h2>Sites</h2><RouterLink to="/sites">Manage sites</RouterLink></div>
      <div class="scroll"><table>
        <thead><tr><th>Site</th><th>State</th><th>Last full backup</th><th></th></tr></thead>
        <tbody>
          <tr v-for="x in list" :key="x.name">
            <td><RouterLink :to="'/sites/' + x.name"><b>{{ x.name }}</b></RouterLink></td>
            <td><Badge :kind="x.maintenance ? 'warn' : 'ok'">{{ x.maintenance ? 'maintenance' : 'online' }}</Badge></td>
            <td>{{ x.latest_backup ? backupTime(x.latest_backup) : 'none' }}</td>
            <td class="r"><a :href="x.url" target="_blank" rel="noopener">Open ERPNext</a></td>
          </tr>
          <tr v-if="!list.length"><td colspan="4" class="hint">{{ sites.data ? 'No sites yet.' : 'Loading…' }}</td></tr>
        </tbody>
      </table></div>
    </section>
    <section v-if="s" class="panel">
      <h2 style="margin-bottom:16px">Server</h2>
      <div class="meters">
        <Meter label="CPU" :pct="s.cpu_pct" :note="`${s.cores} cores, load ${s.load.join(' / ')} (1, 5, 15 min)`" />
        <Meter label="Memory" :pct="s.mem.pct" :note="`${s.mem.used} used of ${s.mem.total}, ${s.mem.available} available`" />
        <Meter v-if="s.swap.total !== '0 B'" label="Swap" :pct="s.swap.pct" :note="`${s.swap.used} used of ${s.swap.total}`" />
        <Meter v-for="d in s.disks" :key="d.label" :label="d.label" :pct="d.pct" :note="`${d.used} used, ${d.free} free of ${d.total}`" />
      </div>
      <p class="hint" style="margin-top:18px">Up for {{ s.uptime }}.</p>
    </section>
  </div>
</template>
