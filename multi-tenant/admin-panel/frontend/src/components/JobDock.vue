<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import Icon from './Icon.vue'
import { termText } from '../termText.js'
import { job, sendInput } from '../useJob.js'

// One job at a time, on every page: a bar at the foot that opens into the terminal.
const out = ref(null), input = ref(null), line = ref('')
const text = computed(() => job.label === null ? '' : termText(job.log || '') || '(no output yet)')

async function toEnd() {
  await nextTick()
  if (out.value) out.value.scrollTop = out.value.scrollHeight
}
// Follow the output, unless the user has scrolled up to read.
watch(text, () => {
  const c = out.value
  if (c && c.scrollTop + c.clientHeight >= c.scrollHeight - 30) toEnd()
})
watch(() => job.open, toEnd)
// A job that waits for an answer must be seen.
watch(() => !!job.interactive, async on => {
  if (!on) return
  job.open = true
  await nextTick()
  input.value?.focus()
})
function send() {
  if (!line.value) return
  sendInput(line.value)
  line.value = ''
}
// Click anywhere to type, but never steal a text selection being copied.
function focusInput() {
  if (job.interactive && !String(document.getSelection())) input.value?.focus()
}
const state = computed(() => job.active ? 'live' : job.label === null ? '' : job.rc === 0 ? 'good' : 'bad')
</script>
<template>
  <section class="dock" :class="[state, { open: job.open }]" aria-label="Terminal">
    <div class="dock-bar">
      <button class="dock-toggle" :aria-expanded="job.open" @click="job.open = !job.open">
        <i class="lamp"></i>
        <span id="jobstate" class="dock-label">
          <template v-if="job.active"><b>{{ job.label || 'Starting' }}</b> running for {{ job.elapsed }}s</template>
          <template v-else-if="job.label === null">No job has run yet</template>
          <template v-else><b>{{ job.label }}</b> {{ job.rc === 0 ? 'finished' : `failed (exit ${job.rc})` }} after {{ job.elapsed }}s, started {{ job.started }}<template v-if="job.hint">. {{ job.hint }}</template></template>
        </span>
        <span class="dock-action">{{ job.open ? 'Hide terminal' : 'Show terminal' }}</span><Icon :name="job.open ? 'down' : 'up'" />
      </button>
      <a v-if="job.logfile" class="dock-dl" :href="'/download/log/' + encodeURIComponent(job.logfile)" download>Download log</a>
    </div>
    <div v-show="job.open" class="term" @click="focusInput">
      <div id="console" ref="out" tabindex="0" role="log" aria-label="Job output">{{ text }}</div>
      <div v-if="job.interactive" class="term-in">
        <span>&gt;</span>
        <input id="term-line" ref="input" v-model="line" aria-label="Type an answer for the job" placeholder="type here, then press Enter"
               autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" @keydown.enter="send">
      </div>
    </div>
  </section>
</template>
