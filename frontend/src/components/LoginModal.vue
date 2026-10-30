<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import api from '../api'
import { apiErrorText } from '../utils/apiError'
import { useEscClose } from '../composables/useEscClose'
import { useModalFocus } from '../composables/useModalFocus'

const emit = defineEmits(['close', 'logged-in'])

const modalRef = ref(null)
useEscClose(() => emit('close'), modalRef)
useModalFocus(modalRef)
const tab = ref('qrcode')
const cookie = ref('')
const qrImage = ref('')
const qrTip = ref('正在加载二维码...')
const loading = ref(false)
const loginError = ref('')
let pollTimer = null
let pollToken = 0
let mounted = false

function currentPoll(token) {
  return mounted && tab.value === 'qrcode' && token === pollToken
}

function stopPoll() {
  clearTimeout(pollTimer)
  pollTimer = null
  pollToken++
}

async function loadQR() {
  if (!mounted || tab.value !== 'qrcode') return
  stopPoll()
  const token = pollToken
  qrImage.value = ''
  loginError.value = ''
  qrTip.value = '正在加载二维码...'
  try {
    const { data } = await api.qrcodeStart()
    if (!currentPoll(token)) return
    qrImage.value = data.qr_image
    qrTip.value = '请用 115 手机 App 扫码'
    startPoll(data.uid, data.time, data.sign)
  } catch (e) {
    if (!currentPoll(token)) return
    qrTip.value = '二维码加载失败：' + apiErrorText(e)
  }
}

function startPoll(uid, time, sign) {
  stopPoll()
  const token = pollToken
  const poll = async () => {
    if (!currentPoll(token)) return
    try {
      const { data } = await api.qrcodePoll(uid, time, sign)
      if (!currentPoll(token)) return
      if (data.status === 1) qrTip.value = '已扫描，请在手机上确认'
      if (data.status === 2) {
        stopPoll()
        qrTip.value = '登录成功'
        emit('logged-in')
        return
      }
      if (data.status === -1 || data.status === -2) {
        stopPoll()
        qrTip.value = data.status === -2 ? '已取消，正在重新加载…' : '二维码已过期，正在重新加载…'
        pollTimer = setTimeout(loadQR, 800)
        return
      }
    } catch (e) {
      if (!currentPoll(token)) return
      qrTip.value = '连接暂时中断，正在重试…'
    }
    if (currentPoll(token)) pollTimer = setTimeout(poll, 2000)
  }
  pollTimer = setTimeout(poll, 1200)
}

async function submitCookie() {
  if (!cookie.value.trim()) return
  loading.value = true
  loginError.value = ''
  try {
    await api.loginCookie(cookie.value.trim())
    emit('logged-in')
  } catch (e) {
    loginError.value = apiErrorText(e)
  } finally {
    loading.value = false
  }
}

function switchTab(t) {
  tab.value = t
  loginError.value = ''
  stopPoll()
  if (t === 'qrcode') loadQR()
}

onMounted(() => { mounted = true; void loadQR() })
onUnmounted(() => { mounted = false; stopPoll() })
</script>

<template>
  <div class="modal-mask" @click.self="emit('close')">
    <div ref="modalRef" class="modal" role="dialog" aria-modal="true"
         aria-labelledby="login-title" tabindex="-1">
      <h3 id="login-title">登录 115 网盘</h3>

      <div class="tabs" role="tablist" aria-label="登录方式">
        <button role="tab" :aria-selected="tab === 'qrcode'"
                :class="{ active: tab === 'qrcode' }" @click="switchTab('qrcode')">扫码登录</button>
        <button role="tab" :aria-selected="tab === 'cookie'"
                :class="{ active: tab === 'cookie' }" @click="switchTab('cookie')">Cookie 登录</button>
      </div>

      <div v-if="tab === 'qrcode'" style="text-align:center">
        <img v-if="qrImage" :src="qrImage" class="qr" alt="二维码" />
        <div v-else class="qr-placeholder">
          二维码加载中
        </div>
        <p style="margin-top:14px;color:var(--muted);font-size:13px">{{ qrTip }}</p>
      </div>

      <div v-else>
        <textarea v-model="cookie"
                  autocomplete="off" spellcheck="false"
                  placeholder="粘贴 115 Cookie，例如&#10;UID=xxx; CID=xxx; SEID=xxx; KID=xxx"></textarea>
        <p style="color:var(--muted-2);font-size:11px;margin:6px 0 14px">
          浏览器登录 115.com 后，F12 → Application → Cookies 复制
        </p>
        <p v-if="loginError" class="form-error" role="alert">{{ loginError }}</p>
        <button class="btn" style="width:100%;justify-content:center" :disabled="loading" @click="submitCookie">
          {{ loading ? '验证中...' : '登录' }}
        </button>
      </div>

      <button class="btn ghost sm" style="width:100%;justify-content:center;margin-top:16px"
              @click="emit('close')">关闭</button>
    </div>
  </div>
</template>
