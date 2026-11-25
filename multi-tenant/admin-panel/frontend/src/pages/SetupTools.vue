<script setup>
import PanelDomain from '../components/PanelDomain.vue'
import SetupFrame from '../components/SetupFrame.vue'
import { job, runAction } from '../useJob.js'
const go = s => runAction('install_tools', { schedule_cron: false }, `Copy the checkout's multi-tenant tools to ${s.tools_dir}?\n\nSettings and cron are not changed.`, { action: s.components.tools.installed ? 'Refresh tools' : 'Install tools' })
</script>
<template>
  <SetupFrame title="Tools" lead="The backup, restore, site and update scripts this panel runs.">
    <template #state="{ c }"><Badge :kind="c.tools.installed ? 'ok' : 'warn'">{{ c.tools.installed ? 'installed' : 'not installed' }}</Badge><Badge v-if="c.tools.conf" kind="ok">config found</Badge></template>
    <template #default="{ s, c }"><section class="panel"><dl class="facts"><div><dt>Scripts</dt><dd>{{ s.tools_dir }}</dd></div><div><dt>Settings (never overwritten by a refresh)</dt><dd>{{ s.mt_conf }}</dd></div></dl>
      <button v-if="c.erp.installed" class="btn" style="margin-top:18px" :disabled="job.active" @click="go(s)">{{ c.tools.installed ? 'Refresh tools from checkout' : 'Install tools' }}</button><p v-else class="hint" style="margin-top:16px"><RouterLink to="/setup/stack">Install the stack</RouterLink> first; it brings the tools.</p></section>
      <PanelDomain v-if="c.erp.installed" :p="s.panel" />
      <p class="hint">To pull new code from git first, use <RouterLink to="/updates">Update panel and tools</RouterLink>.</p></template>
  </SetupFrame>
</template>
