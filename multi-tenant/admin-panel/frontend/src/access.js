import { reactive } from 'vue'
import { runAction } from './useJob.js'

// The site chosen on the Login protection pages.
export const access = reactive({ site: '' })
try { access.site = localStorage.getItem('sspl-auth-site') || '' } catch { /* private mode */ }

export function addNet(site, kind, ip) {
  if (!ip) return false
  return runAction('auth_ip_add', { site, kind, ip }, kind === 'bypass'
    ? `Whitelist ${ip} on ${site}?\n\nEveryone at that address skips the Authelia login and sees the ERPNext login.`
    : `Skip 2FA for ${ip} on ${site}?\n\nEveryone at that address logs in with the password alone.`,
  { action: kind === 'bypass' ? 'Whitelist' : 'Skip 2FA' })
}
