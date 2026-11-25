<script setup>
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import ConfirmDialog from './components/ConfirmDialog.vue'
import Icon from './components/Icon.vue'
import JobDock from './components/JobDock.vue'
import Toasts from './components/Toasts.vue'
import { me, sites, usePoll } from './data.js'
import { toasts } from './ui.js'
import { job, startPolling } from './useJob.js'

const NAV = [
  ['', [['/', 'Overview', 'home']]],
  ['Sites', [['/sites', 'Sites', 'sites'], ['/traffic', 'Site traffic', 'chart'], ['/licenses', 'Licenses', 'license']]],
  ['Backups', [['/backups', 'Backups', 'backup'], ['/uploads', 'Uploads', 'upload'], ['/schedule', 'Schedule', 'clock'], ['/cloud', 'Cloud storage', 'cloud']]],
  ['Server', [['/updates', 'Updates', 'update'], ['/containers', 'Containers', 'box'], ['/memory', 'Memory', 'memory'], ['/database', 'Database memory', 'backup'], ['/activity', 'Activity', 'list']]],
  ['Login protection', [['/access/users', 'Users', 'users'], ['/access/networks', 'Trusted networks', 'shield'], ['/access/logins', 'Login history', 'list']]],
  ['Printing', [['/printers', 'Print servers', 'printer'], ['/tunnel', 'WireGuard tunnel', 'tunnel']]],
  ['Setup', [['/setup/stack', 'ERPNext stack', 'stack'], ['/setup/tools', 'Tools', 'tool'], ['/setup/nginx', 'Domains and HTTPS', 'route'], ['/setup/authelia', 'Authelia and mail', 'shield'], ['/setup/whatsapp', 'WhatsApp API', 'chat']]],
]
const route = useRoute(), menu = ref(false)
const on = to => to === '/' ? route.path === '/' : route.path === to || route.path.startsWith(to + '/') || route.meta.nav === to
watch(() => route.path, () => { menu.value = false; toasts.splice(0); document.querySelector('.content')?.scrollTo(0, 0) })
usePoll(sites.load, 30000)
startPolling()
</script>
<template>
  <div class="shell" :class="{ docked: job.open }">
    <header class="mobile-bar">
      <button aria-label="Menu" :aria-expanded="menu" @click="menu = !menu"><Icon :name="menu ? 'close' : 'menu'" /></button><b>SSPL ERP Admin</b>
    </header>
    <div v-if="menu" class="scrim" @click="menu = false"></div>
    <aside class="side" :class="{ shown: menu }">
      <RouterLink class="brand" to="/"><b>SSPL ERP</b><span>Server admin</span></RouterLink>
      <nav aria-label="Main">
        <div v-for="[group, items] in NAV" :key="group" class="nav-group">
          <h2 v-if="group">{{ group }}</h2>
          <RouterLink v-for="[to, label, icon] in items" :key="to" :to="to" :class="{ on: on(to) }" :aria-current="on(to) ? 'page' : null"><Icon :name="icon" />{{ label }}</RouterLink>
        </div>
      </nav>
      <div class="side-foot">
        <div><b>{{ me.user }}</b><span title="Panel version. Restart the service after updating.">v{{ me.version }}</span></div>
        <form method="post" action="/logout"><button>Log out</button></form>
      </div>
    </aside>
    <main class="content"><div class="page"><RouterView :key="route.path" /></div></main>
    <JobDock />
  </div>
  <Toasts />
  <ConfirmDialog />
</template>
