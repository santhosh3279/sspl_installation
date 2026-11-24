import { reactive } from 'vue'
import { getJSON, postJSON } from './api.js'
import { ask, toast, toasts } from './ui.js'

// The panel runs one job at a time, so this state is shared by the whole app.
// `active` is also set the moment a job is started, before the first poll.
export const job = reactive({
  active: false, interactive: false, label: null, elapsed: 0, rc: null,
  started: '', hint: '', logfile: null, log: '', open: false,
})

const finished = new Set()
export function onJobFinished(fn) {
  finished.add(fn)
  return () => finished.delete(fn)
}

// A poll sent before a job was started must not report it as not running.
let seq = 0
export async function refreshJob() {
  const mine = ++seq
  let j
  try { j = await getJSON('/api/job') } catch { return }
  if (mine !== seq) return
  const was = job.active
  Object.assign(job, j)
  if (was && !j.active) {
    toast(j.rc === 0 ? `${j.label} finished` : `${j.label} failed (exit ${j.rc}). Open the terminal for details.`,
      j.rc === 0 ? 'ok' : 'error')
    finished.forEach(fn => fn())
  }
}

/** Call after the server accepted a job. */
export function jobStarted() {
  seq++
  Object.assign(job, { active: true, open: true })
  refreshJob()
}

/** Start an action: '' when it started, otherwise the error. */
export async function startAction(name, body = {}) {
  try {
    const j = await postJSON('/api/run/' + name, body)
    if (j.error) return j.error
  } catch { return 'The request failed. Check the connection and try again.' }
  jobStarted()
  return ''
}

/** Confirm (optional), start, and show an error as a toast. */
export async function runAction(name, body = {}, confirmText = '', opts = {}) {
  if (job.active) return false
  toasts.splice(0)
  if (confirmText && !await ask(confirmText, opts)) return false
  const error = await startAction(name, body)
  if (error) toast(error, 'error')
  return !error
}

export async function sendInput(line) {
  try {
    const j = await postJSON('/api/job/input', { line })
    if (j.error) toast(j.error, 'error')
  } catch { toast('Could not send the input.', 'error') }
  refreshJob()
}

// Faster while a job runs, so prompts and output feel prompt to type against.
export function startPolling() {
  const tick = async () => {
    await refreshJob()
    setTimeout(tick, job.active ? 1000 : 3000)
  }
  tick()
}
