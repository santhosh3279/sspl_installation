<script setup>
import { ref } from 'vue'
import { enc, me, resource, setup, usePoll } from '../data.js'
import { job, runAction } from '../useJob.js'
const b = resource('/api/backups'), snap = ref('')
usePoll(b.load, 60000); usePoll(setup.load, 30000)
const update = () => runAction('update', {}, 'Update ERPNext for EVERY site now?\n\n1. full backup of every site (the update stops if one fails)\n2. image snapshot for rollback\n3. new images downloaded with every site up\n4. all sites offline for a few minutes while each one is migrated\n\nA site whose migration fails is left in maintenance mode; the others come back.', { action: 'Update all sites' })
const rollback = () => runAction('rollback', snap.value ? { snapshot: snap.value } : {}, 'Roll back the Docker images for EVERY site to the selected snapshot?\n\nAll sites restart.\n\nImages only: a site the update already migrated then runs old code on a new schema. Restore it from the backup the update took.', { action: 'Roll back', danger: true })
const panel = () => runAction('update_panel', {}, 'Pull the latest code, refresh the multi-tenant tools and update this panel now?\n\nSettings, cron and site data are not touched. The panel restarts itself a few seconds after the job finishes. Reload the page then.', { action: 'Update panel and tools' })
</script>
<template>
  <div class="view">
    <PageHead title="Updates" lead="One image serves every site, so ERPNext is updated for all of them together." />
    <section class="panel"><h2>ERPNext</h2><p class="hint">Download the images first to keep the update short: every site stays up while they download.</p>
      <div class="actions"><button class="btn primary" :disabled="job.active" @click="update">Update all sites</button><button class="btn" :disabled="job.active" @click="runAction('download_image')">Download images</button><button class="btn" :disabled="job.active" @click="runAction('migrate', { all: true }, 'Run database migrations on every site now?\n\nUse this to retry after an update failed at the migration step.', { action: 'Migrate all' })">Migrate all</button></div></section>
    <section class="panel"><h2>Roll back</h2><p class="hint">Returns every site to an earlier image. Databases are not rolled back.</p>
      <div class="inline"><label class="field"><span>Image snapshot</span><select id="rb-snap" v-model="snap"><option value="">Latest snapshot</option><option v-for="i in b.data?.images" :key="i.name" :value="i.name">{{ i.name }} ({{ i.size }})</option></select></label><button class="btn danger" :disabled="job.active" @click="rollback">Roll back</button></div></section>
    <section class="panel flush"><div class="panel-top"><h2>Image snapshots</h2></div><div class="scroll tall"><table><thead><tr><th>File</th><th>Size</th><th>Date</th><th></th></tr></thead><tbody><tr v-for="f in b.data?.images" :key="f.name"><td>{{ f.name }} <Badge v-if="f.latest" kind="ok">latest</Badge></td><td>{{ f.size }}</td><td>{{ f.mtime }}</td><td class="r"><a class="btn small" :href="'/download/image/' + enc(f.name)">Download</a></td></tr><tr v-if="!b.data?.images.length"><td colspan="4" class="hint">No snapshots yet. Each update makes one.</td></tr></tbody></table></div></section>
    <section class="panel"><div class="panel-top"><h2>Panel and tools</h2><template v-if="setup.data?.repo_ok"><Badge v-if="setup.data.repo_panel_version && setup.data.repo_panel_version !== setup.data.panel_version" kind="warn">checkout has v{{ setup.data.repo_panel_version }}</Badge><Badge v-else kind="ok">matches the checkout</Badge></template></div>
      <p class="hint">Running v{{ me.version }}. Updating pulls <code v-if="setup.data?.repo_dir">{{ setup.data.repo_dir }}</code><template v-else>the checkout</template> from git, refreshes the tools and redeploys this panel. Settings, cron and site data are not touched.</p>
      <button v-if="me.repoOk" class="btn" style="margin-top:12px" :disabled="job.active" @click="panel">Update panel and tools</button><p v-else class="hint">The updater is not available: the checkout on this server is missing or older.</p></section>
  </div>
</template>
