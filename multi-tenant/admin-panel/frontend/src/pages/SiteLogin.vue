<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import SiteFrame from '../components/SiteFrame.vue'
import LoginToggle from '../components/LoginToggle.vue'
import { sites } from '../data.js'
const name = useRoute().params.site
const users = computed(() => (sites.data?.auth?.users?.[name] || []).filter(u => !u.disabled))
</script>
<template>
  <SiteFrame v-slot="{ s }">
    <p v-if="!sites.data.auth.installed" class="notice">Authelia is not installed. <RouterLink to="/setup/authelia">Install it</RouterLink></p>
    <section v-else class="panel"><h2>Authelia login is {{ s.auth ? 'on' : 'off' }}</h2>
      <p class="hint"><template v-if="s.auth">Every domain and IP + port of this site asks for a login with 2FA first (HTTPS only). {{ users.length }} user(s) may enter.</template>
        <template v-else>Anyone who reaches this site sees the ERPNext login. Turning it on makes the site HTTPS-only and puts a 2FA login in front of every domain and IP + port, with this site's own users ({{ users.length }} so far). API clients, the mobile app, webhooks and payment callbacks are stopped by it too: give those an unlinked domain.</template></p>
      <div class="actions"><LoginToggle :site="name" :linked="s.auth" :users="users.length" :https="!!sites.data.https" /><RouterLink class="btn" :to="{ path: '/access/users', query: { site: name } }">Manage users</RouterLink><RouterLink class="btn" :to="{ path: '/access/networks', query: { site: name } }">Trusted networks</RouterLink></div></section>
  </SiteFrame>
</template>
