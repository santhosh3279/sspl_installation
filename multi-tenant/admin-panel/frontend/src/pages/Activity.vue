<script setup>
import { resource, usePoll } from '../data.js'
const logs = resource('/api/logs')
usePoll(logs.load, 60000)
</script>
<template>
  <div class="view">
    <PageHead title="Activity" lead="Every past run and its log. Live output is in the terminal at the foot of the page." />
    <p v-if="logs.error" class="notice">{{ logs.error }}</p>
    <section class="panel flush"><div class="scroll"><table><thead><tr><th>Run</th><th>Date</th><th>Size</th><th></th></tr></thead><tbody><tr v-for="l in logs.data?.logs" :key="l.name"><td><b>{{ l.label }}</b></td><td>{{ l.mtime }}</td><td>{{ l.size }}</td><td class="r"><a class="btn small" :href="'/download/log/' + encodeURIComponent(l.name)">Download log</a></td></tr><tr v-if="!logs.data?.logs?.length"><td colspan="4" class="hint">{{ logs.data ? 'No runs yet.' : 'Loading…' }}</td></tr></tbody></table></div></section>
  </div>
</template>
