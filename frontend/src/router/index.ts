import { createRouter, createWebHistory } from 'vue-router'

import DictionaryImportView from '@/views/DictionaryImportView.vue'
import Overview from '@/views/Overview.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/term_normalize/dictionary', name: 'term-dictionary', component: DictionaryImportView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
