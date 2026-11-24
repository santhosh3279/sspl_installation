import { createApp } from 'vue'
import '@fontsource-variable/instrument-sans/wght.css'
import './style.css'
import App from './App.vue'
import router from './router.js'
import Badge from './components/Badge.vue'
import Meter from './components/Meter.vue'
import PageHead from './components/PageHead.vue'

createApp(App).use(router).component('Badge', Badge).component('Meter', Meter).component('PageHead', PageHead).mount('#app')
