<script setup>
import { reactive } from 'vue'
import AccessFrame from '../components/AccessFrame.vue'
import RememberedLogins from '../components/RememberedLogins.vue'
import { access, addNet } from '../access.js'
import { auth } from '../data.js'
import { job, runAction } from '../useJob.js'
const KINDS = [['bypass', 'Whitelist', 'No Authelia login from these addresses on this site. The ERPNext login still applies, and everyone behind the address gets it: use fixed addresses you control.', 'Add to whitelist'], ['trusted', 'No 2FA', 'From these addresses the password alone is enough on this site.', 'Add to no-2FA']]
const n = reactive({ bypass: '', trusted: '' })
const all = k => (auth.data['all_' + k] || '').split(/\s+/).filter(Boolean)
async function add(k) { if (await addNet(access.site, k, n[k].trim())) n[k] = '' }
const remove = (kind, ip) => runAction('auth_ip_remove', { site: access.site, kind, ip }, `Remove ${ip} from this list on ${access.site}?`, { action: 'Remove', danger: true })
</script>
<template>
  <AccessFrame v-slot="{ s }" title="Trusted networks" lead="Addresses and browsers that skip part or all of the Authelia login.">
    <section v-for="[k, title, text, button] in KINDS" :key="k" class="panel flush"><div class="panel-top"><div><h2>{{ title }}</h2><p class="hint">{{ text }}</p></div></div>
      <table><tbody><tr v-for="ip in all(k)" :key="'a' + ip"><td><code>{{ ip }}</code> <Badge>all sites</Badge></td><td></td></tr><tr v-for="ip in s.nets[k]" :key="ip"><td><code>{{ ip }}</code></td><td><div class="actions"><button class="btn small danger" :disabled="job.active" @click="remove(k, ip)">Remove</button></div></td></tr><tr v-if="!all(k).length && !s.nets[k].length"><td class="hint">None.</td><td></td></tr></tbody></table>
      <form class="inline" style="padding:16px 22px;border-top:1px solid var(--line)" @submit.prevent="add(k)"><input :id="'n-' + k" v-model="n[k]" aria-label="IP address or network" placeholder="203.0.113.7 or 192.168.1.0/24" autocomplete="off"><button class="btn" :disabled="job.active">{{ button }}</button></form></section>
    <RememberedLogins :site="s.name" />
  </AccessFrame>
</template>
