<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import SiteFrame from '../components/SiteFrame.vue'
import { job, startAction } from '../useJob.js'
const name = useRoute().params.site, router = useRouter()
const f = reactive({ confirm: '', pw: '' }), error = ref(''), busy = ref(false)
async function drop() {
  busy.value = true
  error.value = await startAction('drop_site', { site: name, confirm_site: f.confirm, admin_password: f.pw })
  busy.value = false
  if (!error.value) router.push('/sites')
}
</script>
<template>
  <SiteFrame>
    <form class="panel edge-danger form" style="max-width:none" @submit.prevent="drop"><h2>Drop {{ name }}</h2>
      <p class="notice crit"><b>This deletes the database of {{ name }}.</b> Its users lose access. A full backup is taken first and must succeed, or nothing is dropped. Its backups stay on disk (and in the cloud) afterwards. Remove its DNS entry too.</p>
      <label class="field" style="max-width:420px"><span>Type the site name <b>{{ name }}</b> to confirm</span><input id="dr-confirm" v-model="f.confirm" autocomplete="off"></label>
      <label class="field" style="max-width:420px"><span>Your admin panel password</span><input id="dr-pw" v-model="f.pw" type="password" autocomplete="current-password"></label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button class="btn danger-solid" :disabled="job.active || busy">Back up, then drop</button></form>
  </SiteFrame>
</template>
