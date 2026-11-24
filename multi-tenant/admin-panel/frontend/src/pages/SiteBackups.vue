<script setup>
import { useRoute } from 'vue-router'
import BackupLists from '../components/BackupLists.vue'
import SiteFrame from '../components/SiteFrame.vue'
import { resource, usePoll } from '../data.js'
import { job, runAction } from '../useJob.js'
const name = useRoute().params.site
const b = resource('/api/backups?site=' + encodeURIComponent(name))
usePoll(b.load, 60000)
</script>
<template>
  <SiteFrame>
    <div class="actions"><button class="btn primary" :disabled="job.active" @click="runAction('backup', { site: name })">Full backup</button><button class="btn" :disabled="job.active" @click="runAction('db_backup', { site: name })">Database backup</button></div>
    <p v-if="b.error" class="notice">{{ b.error }}</p>
    <BackupLists :data="b.data" :site="name" />
  </SiteFrame>
</template>
