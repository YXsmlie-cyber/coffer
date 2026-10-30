import axios from 'axios'
import { apiPathPrefix } from '../utils/assetUrl'

const http = axios.create({ baseURL: `${apiPathPrefix()}/api` })
http.interceptors.request.use(config => {
  // 只读页面请求有明确结束边界；同步/抓取等命令沿用各自的后端预算。
  if (config.method === 'get' && !config.timeout) config.timeout = 30000
  return config
})
http.interceptors.response.use(response => response, error => {
  if (error.response?.data?.code === 'REMOTE_AUTH_REQUIRED') {
    window.dispatchEvent(new CustomEvent('ph115:remote-auth-required'))
  }
  return Promise.reject(error)
})

export default http
