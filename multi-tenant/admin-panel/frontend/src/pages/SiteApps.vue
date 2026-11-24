<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import SiteFrame from '../components/SiteFrame.vue'
import { resource, sites, usePoll } from '../data.js'
import { job, runAction } from '../useJob.js'
const name = useRoute().params.site
const apps = resource(`/api/sites/${encodeURIComponent(name)}/apps`)
usePoll(apps.load, 3600000)            // one bench call per read: on open and after a job
const notIn = computed(() => (sites.data?.image_apps || []).filter(a => !apps.data?.apps.includes(a)))
const uninstall = app => runAction('uninstall_app', { site: name, app }, `Uninstall ${app} from ${name}?\n\nThis DELETES ${app}'s DocTypes and all their data on ${name}. A full backup of ${name} is taken first. ${name} is in maintenance mode while it runs; other sites stay up.`, { action: 'Uninstall', danger: true })
</script>
<template>
  <SiteFrame>
    <p v-if="!apps.data" class="hint">{{ apps.error || 'Reading installed apps…' }}</p>
    <p v-else-if="apps.data.error" class="notice crit">{{ apps.data.error }}</p>
    <template v-else>
      <section class="panel flush"><table><thead><tr><th>Installed app</th><th></th></tr></thead><tbody>
        <tr v-for="a in apps.data.apps" :key="a"><td><b>{{ a }}</b></td><td><div class="actions"><button class="btn small danger" :disabled="job.active" @click="uninstall(a)">Uninstall</button></div></td></tr>
        <tr v-if="!apps.data.apps.length"><td colspan="2" class="hint">No apps besides frappe.</td></tr></tbody></table></section>
      <div><p v-if="notIn.length" class="hint">In the image but not on this site: {{ notIn.join(', ') }}<template v-if="apps.data.skip.length">. Never auto-installed: {{ apps.data.skip.join(', ') }}</template></p>
      <p class="hint">Uninstalling deletes that app's data on <b>{{ name }}</b> only, after a full backup of it. Do this for every site that has an app before updating to an image without it; the update lists them.</p></div>
    </template>
  </SiteFrame>
</template>
