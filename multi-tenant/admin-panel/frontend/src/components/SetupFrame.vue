<script setup>
import { setup, usePoll } from '../data.js'
defineProps({ title: String, lead: String })
usePoll(setup.load, 30000)
</script>
<template>
  <div class="view">
    <PageHead :title="title" :lead="lead"><slot v-if="setup.data?.repo_ok" name="state" :c="setup.data.components" /></PageHead>
    <p v-if="setup.error" class="notice">{{ setup.error }}</p><p v-if="!setup.data && !setup.error" class="hint">Loading…</p>
    <p v-if="setup.data && !setup.data.repo_ok" class="notice">Multi-tenant installer scripts not found (<template v-if="setup.data.repo_dir">repo_dir = <code>{{ setup.data.repo_dir }}</code></template><template v-else>repo_dir is not set in config.json</template>). Component installs are unavailable. Check out the <code>Multi-Site</code> branch on this server, set <code>repo_dir</code> in <code>/opt/sspl-admin/config.json</code>, then restart the panel.</p>
    <slot v-else-if="setup.data" :s="setup.data" :c="setup.data.components" />
  </div>
</template>
