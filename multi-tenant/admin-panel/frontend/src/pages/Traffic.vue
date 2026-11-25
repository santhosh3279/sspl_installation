<script setup>
import { computed, ref, watch } from 'vue'
import { resource, usePoll } from '../data.js'
const RANGES = [['24h', 'Last 24 hours'], ['7d', 'Last 7 days'], ['14d', 'Last 14 days']]
const METRICS = [['calls', 'Pages and API calls'], ['addresses', 'Addresses'], ['bytes', 'Data sent'], ['errors', 'Errors']]
const range = ref('24h'), site = ref(''), metric = ref('calls'), tip = ref(-1)
const t = resource('')
const load = () => t.load('/api/traffic?range=' + range.value)
usePoll(load, 60000)
watch(range, () => { t.clear(); load() })
watch([range, site], () => { tip.value = -1 })
const d = computed(() => t.data)
const s = computed(() => d.value && (d.value.sites.find(x => x.name === site.value) || d.value.all))
const vals = computed(() => s.value.series.map(b => b[metric.value]))
const n = x => x.toLocaleString('en')
const size = b => { let i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++ } return +b.toFixed(1) + ' ' + ['B', 'KB', 'MB', 'GB', 'TB'][i] }
const show = v => metric.value === 'bytes' ? size(v) : n(v)
// Axis top: a round number at or above the highest bar, with a round half.
const top = computed(() => {
  const m = Math.max(...vals.value, 2), base = metric.value === 'bytes' ? 1024 ** Math.floor(Math.log(m) / Math.log(1024)) : 1
  const p = 10 ** Math.floor(Math.log10(m / base))
  return base * ([2, 4, 6, 8, 10].find(k => k * p >= m / base) || 10) * p
})
const H = 200
const h = v => H * v / top.value
const step = computed(() => 1000 / vals.value.length)
// The plot is stretched to the page width, so the rounding is only vertical.
const bar = (v, i) => { const w = step.value, x = i * w + w * .12, y = H - h(v), r = Math.min(3, h(v)); return `M${x} ${H}V${y + r}q0 ${-r} ${w * .08} ${-r}H${x + w * .68}q${w * .08} 0 ${w * .08} ${r}V${H}z` }
const ticks = computed(() => { const l = d.value.labels; return [l[0], l[Math.floor(l.length / 2)], l[l.length - 1]] })
const metricName = computed(() => METRICS.find(m => m[0] === metric.value)[1])
</script>
<template>
  <div class="view">
    <PageHead title="Site traffic" lead="Requests that reached each site through this server's nginx." />
    <p v-if="t.error" class="notice">{{ t.error }}</p>
    <p v-if="d && !d.enabled" class="notice">nginx is not recording traffic yet. Run the nginx repair once on <RouterLink to="/setup/nginx">Domains and HTTPS</RouterLink>; counting starts then.</p>
    <div class="inline">
      <label class="field"><span>Period</span><select v-model="range"><option v-for="[k, l] in RANGES" :key="k" :value="k">{{ l }}</option></select></label>
      <label class="field"><span>Site</span><select v-model="site"><option value="">All sites</option><option v-for="x in d?.sites" :key="x.name" :value="x.name">{{ x.name }}</option></select></label>
      <label class="field"><span>Chart</span><select v-model="metric"><option v-for="[k, l] in METRICS" :key="k" :value="k">{{ l }}</option></select></label>
    </div>
    <p v-if="!d && !t.error" class="hint">Loading…</p>
    <template v-if="s">
      <section class="panel"><dl class="facts">
        <div><dt>Pages and API calls</dt><dd>{{ n(s.calls) }}<small>{{ n(s.requests) }} requests with assets and realtime</small></dd></div>
        <div><dt>Addresses</dt><dd>{{ n(s.addresses) }}<small>an office often shares one</small></dd></div>
        <div><dt>Data sent</dt><dd>{{ s.bytes_h }}</dd></div>
        <div><dt>Average response</dt><dd>{{ s.avg_ms === null ? '–' : n(s.avg_ms) + ' ms' }}</dd></div>
        <div><dt>Errors</dt><dd>{{ n(s.e5) }} server<small>{{ n(s.e4) }} client (4xx)</small></dd></div></dl></section>
      <section class="panel"><h2>{{ metricName }} per {{ range === '24h' ? 'hour' : 'day' }}</h2>
        <div class="chart-wrap" style="margin-top:20px"><div class="chart-plot" @mouseleave="tip = -1">
          <span v-for="f in [0, .5, 1]" :key="f" class="chart-y" :style="{ top: (1 - f) * 100 + '%' }">{{ show(top * f) }}</span>
          <svg class="chart" viewBox="0 0 1000 200" preserveAspectRatio="none" role="group" :aria-label="metricName">
            <line v-for="f in [0, .5, 1]" :key="f" class="grid" x1="0" x2="1000" :y1="H * f" :y2="H * f" />
            <path v-for="(v, i) in vals" v-show="v" :key="i" class="bar" :d="bar(v, i)" />
            <rect v-for="(v, i) in vals" :key="'h' + i" class="hit" tabindex="0" role="img" :aria-label="d.labels[i] + ': ' + show(v)" :x="i * step" y="0" :width="step" :height="H" @mouseenter="tip = i" @focus="tip = i" @blur="tip = -1" />
          </svg>
          <div v-if="tip >= 0 && tip < vals.length" class="chart-tip" :style="{ left: Math.min(90, Math.max(10, (tip + .5) * 100 / vals.length)) + '%' }"><b>{{ show(vals[tip]) }}</b>{{ d.labels[tip] }}</div>
        </div><div class="chart-x"><span v-for="l in ticks" :key="l">{{ l }}</span></div></div>
        <details style="margin-top:14px"><summary>Show as table</summary><div class="scroll tall"><table><thead><tr><th>Time</th><th v-for="[k, l] in METRICS" :key="k" class="r">{{ l }}</th></tr></thead><tbody><tr v-for="(b, i) in s.series" :key="i"><td>{{ d.labels[i] }}</td><td class="r">{{ n(b.calls) }}</td><td class="r">{{ n(b.addresses) }}</td><td class="r">{{ b.bytes_h }}</td><td class="r">{{ n(b.errors) }}</td></tr></tbody></table></div></details></section>
      <section v-if="!site" class="panel flush"><div class="panel-top"><h2>By site</h2></div><div class="scroll"><table><thead><tr><th>Site</th><th class="r">Pages and API calls</th><th class="r">Addresses</th><th class="r">Data sent</th><th class="r">Response</th><th class="r">4xx</th><th class="r">5xx</th><th></th></tr></thead><tbody><tr v-for="x in d.sites" :key="x.name"><td><RouterLink :to="'/sites/' + x.name"><b>{{ x.name }}</b></RouterLink></td><td class="r">{{ n(x.calls) }}</td><td class="r">{{ n(x.addresses) }}</td><td class="r">{{ x.bytes_h }}</td><td class="r">{{ x.avg_ms === null ? '–' : n(x.avg_ms) + ' ms' }}</td><td class="r">{{ n(x.e4) }}</td><td class="r">{{ n(x.e5) }}</td><td><div class="actions"><button class="btn small" @click="site = x.name">Show</button></div></td></tr><tr v-if="!d.sites.length"><td colspan="8" class="hint">No sites yet.</td></tr></tbody></table></div></section>
      <div class="split"><section v-for="[title, rows, what] in [['Top pages and API methods', s.paths, 'Calls'], ['Top addresses', s.ips, 'Requests']]" :key="title" class="panel flush"><div class="panel-top"><h2>{{ title }}</h2></div><div class="scroll"><table><thead><tr><th>{{ what === 'Calls' ? 'Path' : 'Address' }}</th><th class="r">{{ what }}</th></tr></thead><tbody><tr v-for="[k, c] in rows" :key="k"><td><code>{{ k }}</code></td><td class="r">{{ n(c) }}</td></tr><tr v-if="!rows.length"><td colspan="2" class="hint">Nothing in this period.</td></tr></tbody></table></div></section></div>
      <p v-if="d.unknown" class="hint">{{ n(d.unknown) }} requests for addresses that belong to no site are not counted above. Most are scanners.</p>
    </template>
  </div>
</template>
