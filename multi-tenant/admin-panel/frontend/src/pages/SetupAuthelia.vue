<script setup>
import { reactive, ref, watch } from 'vue'
import SetupFrame from '../components/SetupFrame.vue'
import { setup } from '../data.js'
import { job, runAction } from '../useJob.js'
const f = reactive({ user: '', pw: '', from: '', trusted: '' }), test = ref('')
let seeded = false
watch(() => setup.data, s => {
  const a = s?.components?.auth
  if (!a || seeded) return
  Object.assign(f, { user: a.mail || '', from: a.mail_from && a.mail_from !== a.mail ? a.mail_from : '', trusted: a.trusted || '' })
  seeded = true
}, { immediate: true })
async function save(a) {
  if (await runAction('install_auth', { smtp_user: f.user.trim(), smtp_password: f.pw, mail_from: f.from.trim(), trusted: f.trusted.trim() }, a.installed
    ? `Apply these Authelia / mail settings?\n\nAuthelia may restart for a few seconds.${a.legacy ? ' Every site gets its own users and 2FA: passwords carry over, everyone logs in and registers 2FA again.' : ' Nobody is logged out.'}`
    : 'Install Authelia, Redis and the mail relay now?\n\nNothing is gated until you require the login on a site.', { action: a.installed ? 'Save settings' : 'Install Authelia' })) f.pw = ''
}
const mail = () => { if (test.value.trim()) runAction('auth_mail_test', { to: test.value.trim() }) }
</script>
<template>
  <SetupFrame title="Authelia and mail" lead="A login with 2FA (authenticator app or security key) in front of ERPNext, for the sites you choose. Nothing is gated until you turn it on for a site.">
    <template #state="{ c }"><Badge :kind="c.auth?.installed ? 'ok' : 'warn'">{{ c.auth?.installed ? 'installed' : 'not installed' }}</Badge></template>
    <template #default="{ s, c }">
      <div class="stack-list"><p v-for="x in c.auth?.installed ? c.auth.down : []" :key="x" class="notice crit">{{ x }}: not answering</p>
        <p v-if="c.auth?.installed && c.auth.legacy" class="notice">One login for all sites. Save settings gives each site its own.</p>
        <p v-if="c.auth?.installed && !c.proxy.https_live" class="notice crit">HTTPS is off on the nginx routing: linked sites refuse every request until it works. <RouterLink to="/setup/nginx">Open HTTPS setup</RouterLink></p></div>
      <section v-if="c.auth?.installed" class="panel"><dl class="facts"><div><dt>Sites with the login</dt><dd>{{ (c.auth.linked || []).length }}</dd></div><div><dt>Mail account</dt><dd>{{ c.auth.mail || 'none' }}<small v-if="c.auth.mail">sends through {{ c.auth.relay }}</small></dd></div></dl></section>
      <form v-if="c.erp.installed && s.scripts.auth" class="panel form" style="max-width:none" @submit.prevent="save(c.auth || {})"><h2>Settings</h2>
        <div class="form wide"><label class="field"><span>Gmail address</span><input id="au-smtp-user" v-model="f.user" type="email" autocomplete="off"></label><label class="field"><span>Gmail app password</span><input id="au-smtp-pw" v-model="f.pw" type="password" :placeholder="c.auth?.mail ? 'unchanged' : ''" autocomplete="new-password"></label><label class="field"><span>Sender</span><input id="au-from" v-model="f.from" type="email" placeholder="default: the Gmail address" autocomplete="off"></label><label class="field"><span>No-2FA networks</span><input id="au-trusted" v-model="f.trusted" placeholder="192.168.1.0/24"></label></div>
        <p class="hint">From the no-2FA networks the password is enough, on every site. Space-separated. Empty: 2FA everywhere.</p>
        <p class="hint">Gmail: turn on 2-Step Verification for the Google account, then create an <b>app password</b> (Google Account, Security, App passwords). Gmail sends as that address or a verified alias only.</p>
        <button class="btn primary" :disabled="job.active">{{ c.auth?.installed ? 'Save settings' : 'Install Authelia' }}</button></form>
      <p v-else class="notice">Install the ERPNext stack first. <RouterLink to="/setup/stack">Open setup</RouterLink></p>
      <form v-if="c.erp.installed && s.scripts.auth && c.auth?.installed && c.auth.mail" class="panel" @submit.prevent="mail"><h2 style="margin-bottom:14px">Test the mail</h2><div class="inline"><label class="field"><span>Send a test mail to</span><input id="au-test-to" v-model="test" type="email"></label><button class="btn" :disabled="job.active">Send test mail</button></div></form>
      <p class="hint">Every site has <b>its own users, passwords and 2FA</b>. <b>Remember me</b> keeps that browser logged in for {{ c.auth?.remember || '1y' }} (survives restarts). <RouterLink to="/access/users">Add users</RouterLink>, then turn the login on. Each linked site uses about 30–90 MB of RAM. Needs HTTPS.</p>
    </template>
  </SetupFrame>
</template>
