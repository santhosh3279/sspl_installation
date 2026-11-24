<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { siteByName, sites } from '../data.js'
const route = useRoute(), name = computed(() => route.params.site), s = computed(() => siteByName(name.value))
const TABS = [['', 'Summary'], ['/domains', 'Domains'], ['/apps', 'Apps'], ['/backups', 'Backups'], ['/login', 'Login protection'], ['/drop', 'Drop site']]
</script>
<template>
  <div class="view">
    <PageHead :title="name" back="/sites" back-label="sites">
      <template v-if="s"><Badge :kind="s.maintenance ? 'warn' : 'ok'">{{ s.maintenance ? 'maintenance' : 'online' }}</Badge><Badge v-if="s.auth" kind="ok">login required</Badge><a class="btn" :href="s.url" target="_blank" rel="noopener">Open ERPNext</a></template>
    </PageHead>
    <nav class="tabs" aria-label="Site"><RouterLink v-for="[p, l] in TABS" :key="p" :to="'/sites/' + name + p" :class="{ on: route.path === '/sites/' + name + p }">{{ l }}</RouterLink></nav>
    <slot v-if="s" :s="s" />
    <p v-else class="notice">{{ sites.data ? 'There is no site with this name.' : sites.error || 'Loading…' }}</p>
  </div>
</template>
