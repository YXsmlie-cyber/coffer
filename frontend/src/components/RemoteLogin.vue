<script setup>
import { nextTick, onMounted, ref } from 'vue'
import api from '../api'

const props = defineProps({
  rememberDays: { type: Number, default: 30 },
})
const emit = defineEmits(['authenticated'])

const username = ref(localStorage.getItem('ph115_remote_username') || '')
const password = ref('')
const remember = ref(true)
const showPassword = ref(false)
const loading = ref(false)
const error = ref('')
const usernameRef = ref(null)
const passwordRef = ref(null)

onMounted(() => (username.value ? passwordRef.value : usernameRef.value)?.focus())

async function submit() {
  if (!username.value.trim() || !password.value) {
    error.value = '请输入账号和密码'
    await nextTick()
    const emptyField = !username.value.trim() ? usernameRef.value : passwordRef.value
    emptyField?.focus()
    return
  }
  loading.value = true
  error.value = ''
  let refocusPassword = false
  try {
    const { data } = await api.remoteLogin(
      username.value.trim(), password.value, remember.value)
    if (remember.value) {
      localStorage.setItem('ph115_remote_username', username.value.trim())
    } else {
      localStorage.removeItem('ph115_remote_username')
    }
    password.value = ''
    emit('authenticated', data)
  } catch (e) {
    if (e.response?.data?.code === 'REMOTE_LOGIN_FAILED' || e.response?.status === 401) {
      error.value = '账号或密码不正确，请检查后重试'
    } else if (e.response?.status === 429) {
      error.value = '尝试次数过多，请稍后再试'
    } else {
      error.value = e.response?.data?.detail || '暂时无法登录，请检查网络后重试'
    }
    refocusPassword = true
  } finally {
    loading.value = false
  }
  if (refocusPassword) {
    await nextTick()
    passwordRef.value?.focus()
    passwordRef.value?.select()
  }
}
</script>

<template>
  <main class="remote-auth-shell">
    <div class="remote-auth-glow one" aria-hidden="true"></div>
    <div class="remote-auth-glow two" aria-hidden="true"></div>

    <section class="remote-auth-wrap" aria-labelledby="remote-login-title">
      <div class="remote-auth-brand" aria-label="PH 115">
        <span>PH · 115</span>
        <small><i></i> 安全访问</small>
      </div>

      <div class="remote-auth-card">
        <div class="remote-auth-lock" aria-hidden="true">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
               stroke-linejoin="round">
            <rect x="4" y="10" width="16" height="11" rx="3" />
            <path d="M8 10V7a4 4 0 0 1 8 0v3" />
            <path d="M12 14v3" />
          </svg>
        </div>
        <p class="remote-auth-kicker">远程控制台</p>
        <h1 id="remote-login-title">欢迎回来</h1>
        <p class="remote-auth-intro">验证身份后继续管理下载与上传任务。</p>

        <form class="remote-auth-form" @submit.prevent="submit" novalidate>
          <div class="auth-field">
            <label for="remote-username">账号</label>
            <input id="remote-username" ref="usernameRef" v-model="username"
                   name="username" type="text" autocomplete="username"
                   autocapitalize="none" spellcheck="false" enterkeyhint="next"
                   :aria-invalid="!!error" :disabled="loading" />
          </div>

          <div class="auth-field">
            <label for="remote-password">密码</label>
            <div class="auth-password-wrap">
              <input id="remote-password" ref="passwordRef" v-model="password"
                     name="password" :type="showPassword ? 'text' : 'password'"
                     autocomplete="current-password" enterkeyhint="go"
                     :aria-invalid="!!error" aria-describedby="remote-login-help remote-login-error"
                     :disabled="loading" />
              <button type="button" class="auth-password-toggle"
                      :aria-label="showPassword ? '隐藏密码' : '显示密码'"
                      :title="showPassword ? '隐藏密码' : '显示密码'"
                      @click="showPassword = !showPassword">
                <svg v-if="!showPassword" width="20" height="20" viewBox="0 0 24 24"
                     fill="none" stroke="currentColor" stroke-width="1.8"
                     stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z" />
                  <circle cx="12" cy="12" r="2.5" />
                </svg>
                <svg v-else width="20" height="20" viewBox="0 0 24 24"
                     fill="none" stroke="currentColor" stroke-width="1.8"
                     stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="m3 3 18 18" />
                  <path d="M10.6 6.2A11.8 11.8 0 0 1 12 6c6.5 0 10 6 10 6a17 17 0 0 1-2.1 2.8" />
                  <path d="M6.6 6.6C3.6 8.4 2 12 2 12s3.5 6 10 6c1.2 0 2.3-.2 3.3-.6" />
                </svg>
              </button>
            </div>
          </div>

          <label class="remote-auth-remember">
            <input v-model="remember" type="checkbox" :disabled="loading" />
            <span>在此设备上保持登录 {{ rememberDays }} 天</span>
          </label>

          <p id="remote-login-error" class="remote-auth-error" role="alert" aria-live="assertive">
            {{ error }}
          </p>

          <button class="btn remote-auth-submit" type="submit" :disabled="loading">
            <span v-if="loading" class="auth-spinner" aria-hidden="true"></span>
            {{ loading ? '正在验证…' : '进入控制台' }}
          </button>
        </form>

        <p id="remote-login-help" class="remote-auth-help">
          密码仅通过 HTTPS 交给服务器验证，应用不会保存。
        </p>
      </div>
    </section>
  </main>
</template>
