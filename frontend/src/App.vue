<script setup>
import { nextTick, ref, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import api, { pauseWSForAuth, resumeWSAfterAuth } from './api'
import LoginModal from './components/LoginModal.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import RemoteLogin from './components/RemoteLogin.vue'
import ConnectionStatus from './components/ConnectionStatus.vue'
import TaskTray from './components/TaskTray.vue'
import DocumentPulse from './components/DocumentPulse.js'
import KeyboardShortcuts from './components/KeyboardShortcuts.js'
import { apiPathPrefix } from './utils/assetUrl'

const route = useRoute()
const cloudOn = ref(false)
const showLogin = ref(false)
const authReady = ref(false)
const authRequired = ref(false)
const remoteAuthenticated = ref(false)
const authError = ref('')
const rememberDays = ref(30)

async function refreshStatus() {
  try {
    const { data } = await api.cloudStatus()
    cloudOn.value = data.logged_in
  } catch (_) { cloudOn.value = false }
}

function onLoggedIn() {
  showLogin.value = false
  refreshStatus()
}

async function loadRemoteAuth() {
  authReady.value = false
  authError.value = ''
  try {
    const { data } = await api.remoteAuthStatus()
    authRequired.value = !!data.required
    remoteAuthenticated.value = !!data.authenticated
    rememberDays.value = data.remember_days || 30
    if (remoteAuthenticated.value) {
      resumeWSAfterAuth()
      // 115 状态不是进入应用的认证门槛，不阻塞首屏。
      void refreshStatus()
    } else {
      pauseWSForAuth()
    }
  } catch (e) {
    if (e.response?.status === 404) {
      // 无停机更新期间，旧后端进程尚未注册远程会话接口。此时仍沿用原本的
      // 本机直连 / Nginx 全路径 Basic Auth，避免仅更新 dist 就中断运行中任务。
      authRequired.value = false
      remoteAuthenticated.value = true
      resumeWSAfterAuth()
      void refreshStatus()
      return
    }
    authError.value = e.response?.data?.detail || '无法连接 PH-115 服务，请确认程序正在运行'
    pauseWSForAuth()
  } finally {
    authReady.value = true
  }
}

function onRemoteAuthenticated() {
  authRequired.value = true
  remoteAuthenticated.value = true
  authError.value = ''
  resumeWSAfterAuth()
  refreshStatus()
}

function requireRemoteAuth() {
  authReady.value = true
  authRequired.value = true
  remoteAuthenticated.value = false
  showLogin.value = false
  pauseWSForAuth()
}

async function remoteLogout() {
  try { await api.remoteLogout() } catch (_) {}
  requireRemoteAuth()
}

onMounted(() => {
  window.addEventListener('ph115:remote-auth-required', requireRemoteAuth)
  loadRemoteAuth()
})
onUnmounted(() => window.removeEventListener('ph115:remote-auth-required', requireRemoteAuth))

watch(() => route.name, async () => {
  await nextTick()
  document.getElementById('main-content')?.focus({ preventScroll: true })
})
</script>

<template>
  <main v-if="!authReady" class="remote-auth-shell remote-auth-loading" aria-busy="true">
    <span class="auth-spinner" aria-hidden="true"></span>
    <p>正在建立安全连接…</p>
  </main>

  <main v-else-if="authError" class="remote-auth-shell">
    <section class="remote-auth-card remote-auth-connect-error" role="alert">
      <h1>暂时无法连接</h1>
      <p>{{ authError }}</p>
      <button class="btn" @click="loadRemoteAuth">重新连接</button>
    </section>
  </main>

  <RemoteLogin v-else-if="authRequired && !remoteAuthenticated"
               :remember-days="rememberDays" @authenticated="onRemoteAuthenticated" />

  <template v-else>
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <nav class="nav" aria-label="主导航">
      <div class="nav-primary">
        <span class="brand">PH · 115</span>
        <router-link to="/models" :class="{ active: route.path === '/models' }">模特</router-link>
        <router-link to="/library" :class="{ active: route.path === '/library' }">115模特</router-link>
        <router-link to="/discovery" :class="{ active: route.path === '/discovery' }">视频</router-link>
        <router-link to="/taste" :class="{ active: route.path === '/taste' }">探索</router-link>
        <router-link to="/downloads" :class="{ active: route.path === '/downloads' }">下载</router-link>
        <router-link to="/uploads" :class="{ active: route.path === '/uploads' }">上传</router-link>
      </div>
      <div class="nav-account">
        <ConnectionStatus />
        <a class="btn ghost sm" :href="`${apiPathPrefix()}/api/diagnostics`"
           download="ph115-diagnostics.json" title="下载脱敏运行诊断">诊断</a>
        <span class="cloud-status" :class="cloudOn ? 'on' : 'off'" role="status" aria-live="polite">
          115 {{ cloudOn ? '已登录' : '未登录' }}
        </span>
        <button class="btn ghost sm" @click="showLogin = true">
          {{ cloudOn ? '切换' : '登录' }}
        </button>
        <button v-if="authRequired" class="btn ghost sm remote-logout"
                title="退出此外网设备" @click="remoteLogout">退出外网</button>
      </div>
    </nav>

    <main id="main-content" class="container" :class="{ 'container-wide': route.meta.wide }"
          tabindex="-1">
      <router-view v-slot="{ Component, route: activeRoute }">
        <!-- 缓存两个浏览页以保留来源、筛选与滚动位置；任务页仍按原逻辑销毁。 -->
        <KeepAlive include="Models,Library,Discovery" :max="3">
          <component :is="Component" :key="activeRoute.name" />
        </KeepAlive>
      </router-view>
    </main>

    <TaskTray />
    <DocumentPulse />
    <KeyboardShortcuts />
    <LoginModal v-if="showLogin" @close="showLogin = false" @logged-in="onLoggedIn" />
    <ConfirmDialog />
  </template>
</template>
