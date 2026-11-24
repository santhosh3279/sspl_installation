<script setup>
import { reactive, ref } from 'vue'
import { toast } from '../ui.js'
import { job, runAction } from '../useJob.js'
const p = defineProps({ s: Object })
const blank = { user: '', name: '', email: '', pw: '', pw2: '' }, f = reactive({ ...blank })
const edit = reactive({ user: '', pw: '', pw2: '' })
async function add() {
  if (!f.user.trim()) return toast('Enter a user name.', 'error')
  if (f.pw !== f.pw2) return toast('The passwords do not match.', 'error')
  if (await runAction('auth_user_add', { site: p.s.name, user: f.user.trim().toLowerCase(), name: f.name.trim(), email: f.email.trim(), password: f.pw })) Object.assign(f, blank)
}
async function passwd() {
  if (edit.pw !== edit.pw2) return toast('The passwords do not match.', 'error')
  if (await runAction('auth_user_passwd', { site: p.s.name, user: edit.user, password: edit.pw })) Object.assign(edit, { user: '', pw: '', pw2: '' })
}
const reset = user => runAction('auth_user_reset_2fa', { site: p.s.name, user }, `Forget ${user}'s 2FA devices on ${p.s.name}?\n\nThey register again at the next login.`, { action: 'Reset 2FA' })
const remove = user => runAction('auth_user_remove', { site: p.s.name, user }, `Remove ${user} from ${p.s.name}?\n\nThis cannot be undone.`, { action: 'Remove', danger: true })
const toggle = u => runAction('auth_user_disable', { site: p.s.name, user: u.user, on: !u.disabled }, u.disabled ? '' : `Disable ${u.user} on ${p.s.name}?\n\nThey cannot log in until enabled again.`, { action: 'Disable', danger: true })
</script>
<template>
  <section class="panel flush"><div class="scroll"><table><thead><tr><th>User</th><th>E-mail</th><th></th></tr></thead><tbody>
    <tr v-for="u in s.users" :key="u.user"><td><b>{{ u.user }}</b> <Badge v-if="u.disabled" kind="warn">disabled</Badge><div class="sub">{{ u.name }}</div></td><td>{{ u.email }}</td><td><div class="actions"><button class="btn small" :disabled="job.active" @click="Object.assign(edit, { user: u.user, pw: '', pw2: '' })">Set password</button><button class="btn small" :disabled="job.active" title="Lost phone: register 2FA again at the next login" @click="reset(u.user)">Reset 2FA</button><button class="btn small" :disabled="job.active" @click="toggle(u)">{{ u.disabled ? 'Enable' : 'Disable' }}</button><button class="btn small danger" :disabled="job.active" @click="remove(u.user)">Remove</button></div></td></tr>
    <tr v-if="!s.users.length"><td colspan="3" class="hint">No users on this site yet.</td></tr></tbody></table></div></section>
  <form v-if="edit.user" class="panel" @submit.prevent="passwd"><h2 style="margin-bottom:14px">New password for {{ edit.user }} on {{ s.name }}</h2><div class="form wide"><label class="field"><span>Password (8 or more characters)</span><input id="e-pw" v-model="edit.pw" type="password" autocomplete="new-password"></label><label class="field"><span>Confirm password</span><input id="e-pw2" v-model="edit.pw2" type="password" autocomplete="new-password"></label></div><div class="actions" style="margin-top:16px"><button class="btn primary" :disabled="job.active">Set password</button><button type="button" class="btn" @click="edit.user = ''">Cancel</button></div></form>
  <form class="panel" @submit.prevent="add"><h2 style="margin-bottom:14px">Add a user</h2><div class="form wide"><label class="field"><span>User name</span><input id="u-user" v-model="f.user" placeholder="alex" autocomplete="off" autocapitalize="off" spellcheck="false"></label><label class="field"><span>Display name</span><input id="u-name" v-model="f.name" autocomplete="off"></label><label class="field"><span>E-mail (2FA codes)</span><input id="u-email" v-model="f.email" type="email" autocomplete="off"></label><label class="field"><span>Password (8 or more characters)</span><input id="u-pw" v-model="f.pw" type="password" autocomplete="new-password"></label><label class="field"><span>Confirm password</span><input id="u-pw2" v-model="f.pw2" type="password" autocomplete="new-password"></label></div><button class="btn primary" style="margin-top:16px" :disabled="job.active">Add user</button></form>
</template>
