import { createRouter, createWebHashHistory } from 'vue-router'

// Hash routes: Flask only knows "/", and needs no route per page.
const eager = import.meta.glob('./pages/*.vue', { eager: true, import: 'default' })
const page = n => eager[`./pages/${n}.vue`]
const r = (path, name, title, nav) => ({ path, component: page(name), meta: { title, nav } })
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    r('/', 'Overview', 'Overview'),
    r('/sites', 'Sites', 'Sites'), r('/new-site', 'NewSite', 'Create site', '/sites'),
    r('/sites/:site', 'SiteSummary', 'Site'), r('/sites/:site/domains', 'SiteDomains', 'Domains'), r('/sites/:site/apps', 'SiteApps', 'Apps'),
    r('/sites/:site/backups', 'SiteBackups', 'Site backups'), r('/sites/:site/login', 'SiteLogin', 'Login protection'), r('/sites/:site/drop', 'SiteDrop', 'Drop site'),
    r('/licenses', 'Licenses', 'Licenses'),
    r('/backups', 'Backups', 'Backups'), r('/restore', 'Restore', 'Restore', '/backups'), r('/uploads', 'Uploads', 'Uploads'), r('/schedule', 'Schedule', 'Schedule'), r('/cloud', 'Cloud', 'Cloud storage'),
    r('/updates', 'Updates', 'Updates'), r('/containers', 'Containers', 'Containers'), r('/memory', 'Memory', 'Memory'), r('/database', 'Database', 'Database memory'), r('/activity', 'Activity', 'Activity'),
    r('/access/users', 'AccessUsers', 'Users'), r('/access/networks', 'AccessNetworks', 'Trusted networks'), r('/access/logins', 'AccessLogins', 'Login history'),
    r('/printers', 'Printers', 'Print servers'), r('/tunnel', 'Tunnel', 'WireGuard tunnel'),
    r('/setup/stack', 'SetupStack', 'ERPNext stack'), r('/setup/tools', 'SetupTools', 'Tools'), r('/setup/nginx', 'SetupNginx', 'Domains and HTTPS'), r('/setup/authelia', 'SetupAuthelia', 'Authelia and mail'), r('/setup/whatsapp', 'SetupWhatsapp', 'WhatsApp API'),
    r('/:rest(.*)*', 'NotFound', 'Not found'),
  ],
})
router.afterEach(to => { document.title = `${to.params.site ? to.params.site + ': ' : ''}${to.meta.title} - SSPL ERP Admin` })
export default router
