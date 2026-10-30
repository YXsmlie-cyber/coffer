import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './style.css'

// 顶层页面按路由拆包；首屏只加载认证外壳和当前页面。
const Models = () => import('./views/Models.vue')
const Library = () => import('./views/Library.vue')
const Discovery = () => import('./views/Discovery.vue')
const Taste = () => import('./views/Taste.vue')
const Downloads = () => import('./views/Downloads.vue')
const Uploads = () => import('./views/Uploads.vue')

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/models' },
    { path: '/models', name: 'models', component: Models, meta: { wide: true } },
    { path: '/library', name: 'library', component: Library },
    { path: '/discovery', name: 'discovery', component: Discovery },
    { path: '/taste', name: 'taste', component: Taste },
    { path: '/downloads', name: 'downloads', component: Downloads },
    { path: '/uploads', name: 'uploads', component: Uploads },
  ],
  scrollBehavior(to) {
    // 模特页由缓存组件按“模特 ID + 视口偏移”精确恢复；其它顶层页面独立从顶部进入。
    if (to.name === 'models' || to.name === 'library' || to.name === 'discovery') return false
    return { left: 0, top: 0 }
  },
})

createApp(App).use(router).mount('#app')
