<script setup>
import { toast } from '../ui.js'
import { job, runAction } from '../useJob.js'
const p = defineProps({ site: String, linked: Boolean, users: Number, https: Boolean })
function go() {
  const site = p.site
  if (p.linked) return runAction('auth_unlink', { site }, `Remove the Authelia login from ${site}?\n\nAnyone who reaches it sees the ERPNext login again.`, { action: 'Remove the login', danger: true })
  if (!p.users) return toast(`Add a user for ${site} first, or nobody can enter.`, 'error')
  runAction('auth_link', { site }, `Require an Authelia login on ${site}?\n\nThe site becomes HTTPS-only. API clients, the mobile app and webhooks are stopped too, except from whitelisted IPs.`, { action: 'Require login' })
}
</script>
<template><button class="btn" :class="linked ? 'danger' : 'primary'" :disabled="job.active || (!linked && !https)" :title="!linked && !https ? 'HTTPS is off' : null" @click="go">{{ linked ? 'Remove the login' : 'Require Authelia login' }}</button></template>
