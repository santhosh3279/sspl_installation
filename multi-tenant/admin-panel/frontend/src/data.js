import { onMounted, onUnmounted, reactive } from 'vue'
import { getJSON, postJSON } from './api.js'
import { onJobFinished } from './useJob.js'

/** A GET endpoint as reactive state. A late answer never replaces a newer one. */
export function resource(url) {
  const r = reactive({ data: null, error: '' })
  let seq = 0
  r.load = async (u = url) => {
    const mine = ++seq
    try {
      const j = await getJSON(u)
      if (mine === seq) { r.data = j; r.error = '' }
    } catch {
      if (mine === seq) r.error = 'Could not load. What you see may be out of date.'
    }
  }
  r.clear = () => { seq++; r.data = null; r.error = '' }
  return r
}

/** Load now, every `ms` while the page is visible, and after each job. */
export function usePoll(fn, ms) {
  let timer, off
  onMounted(() => {
    fn()
    off = onJobFinished(() => fn())
    timer = setInterval(() => { if (!document.hidden) fn() }, ms)
  })
  onUnmounted(() => { clearInterval(timer); off() })
}

export const sites = resource('/api/sites')
export const stats = resource('/api/stats')
export const setup = resource('/api/setup-status')
export const auth = resource('/api/auth')
export const wg = resource('/api/wireguard')
export const ram = resource('/api/ram')

const root = document.getElementById('app').dataset
export const me = reactive({ user: root.user || '', version: root.version || '', repoOk: root.repoOk !== '0' })
if (!me.user) getJSON('/api/me').then(j => Object.assign(me, j)).catch(() => {})

export const siteNames = () => (sites.data?.sites || []).map(s => s.name)
export const siteByName = name => sites.data?.sites.find(s => s.name === name)
export const backupTime = t => (t || '').replace(/^(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d).*/, '$1-$2-$3 $4:$5')
export const enc = s => encodeURIComponent(s).replace(/%2F/g, '/')

export async function clearRam(say) {
  say('Clearing…')
  try {
    const j = await postJSON('/api/clear-ram', {})
    say(j.error || `Freed ${j.freed}. ${j.available} is now available.`)
  } catch { say('The request failed.') }
}
export const CLEAR_RAM = 'Clear RAM caches now?\n\nThis is safe but may briefly slow the system while caches rebuild.'

/** Upload with progress. `say` gets (text, isError). Resolves to the answer, or null. */
export function upload(url, form, say) {
  return new Promise(resolve => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', url)
    xhr.upload.onprogress = e => { if (e.lengthComputable) say(`Uploading… ${Math.round(100 * e.loaded / e.total)}%`) }
    xhr.onload = () => {
      try {
        const j = JSON.parse(xhr.responseText)
        if (j.error) say(j.error, true)
        resolve(j.error ? null : j)
      } catch { say(`Upload failed (${xhr.status})`, true); resolve(null) }
    }
    xhr.onerror = () => { say('Upload failed: network error', true); resolve(null) }
    xhr.send(form)
    say('Uploading… 0%')
  })
}
