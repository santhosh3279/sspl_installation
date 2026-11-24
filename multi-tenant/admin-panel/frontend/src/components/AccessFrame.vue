<script setup>
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import LoginToggle from './LoginToggle.vue'
import { auth, usePoll } from '../data.js'
import { access } from '../access.js'
defineProps({ title: String, lead: String })
const route = useRoute(), router = useRouter()
usePoll(auth.load, 60000)
const s = computed(() => auth.data?.sites.find(x => x.name === access.site))
watch(() => auth.data, a => {
  if (!a) return
  const want = route.query.site
  if (want && a.sites.some(x => x.name === want)) access.site = want
  else if (!s.value) access.site = a.sites[0]?.name || ''
}, { immediate: true })
watch(() => access.site, v => { try { localStorage.setItem('sspl-auth-site', v) } catch { /* private mode */ } })
function pick(e) { access.site = e.target.value; if (route.query.site) router.replace(route.path) }
</script>
<template>
  <div class="view">
    <PageHead :title="title" :lead="lead" />
    <p v-if="auth.error" class="notice">{{ auth.error }}</p>
    <template v-if="auth.data">
      <p v-if="!auth.data.installed" class="notice">Authelia is not installed. <RouterLink to="/setup/authelia">Install it</RouterLink></p>
      <p v-else-if="!s" class="notice">No sites yet. <RouterLink to="/new-site">Create one</RouterLink></p>
      <template v-else>
        <div class="inline"><label class="field"><span>Site</span><select id="site" :value="access.site" @change="pick"><option v-for="x in auth.data.sites" :key="x.name" :value="x.name">{{ x.name }}{{ x.linked ? ' (login on)' : '' }}</option></select></label><LoginToggle :site="s.name" :linked="s.linked" :users="s.users.length" :https="auth.data.https" /></div>
        <p v-if="!s.linked" class="notice">The login is off for this site: users and IP lists take effect once it is on.</p>
        <div :key="s.name" class="view"><slot :s="s" /></div>
      </template>
    </template>
  </div>
</template>
