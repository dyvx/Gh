import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

// 用于文件上传的实例（不设 Content-Type，让浏览器自动设置 multipart boundary）
const uploadApi = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
})

// 用 refresh token 换新的 access token。并发的多个 401 共用同一次刷新请求。
let refreshing: Promise<string | null> | null = null

export function refreshAccessToken(): Promise<string | null> {
  if (refreshing) return refreshing
  const refreshToken = localStorage.getItem('refresh_token')
  if (!refreshToken) return Promise.resolve(null)
  refreshing = axios
    .post('/api/v1/auth/refresh', { refresh_token: refreshToken })
    .then(({ data }) => {
      localStorage.setItem('access_token', data.access_token)
      localStorage.setItem('refresh_token', data.refresh_token)
      return data.access_token as string
    })
    .catch(() => null)
    .finally(() => {
      refreshing = null
    })
  return refreshing
}

// 登录状态彻底失效：清除令牌并回到登录页
export function forceLogout() {
  localStorage.removeItem('access_token')
  localStorage.removeItem('refresh_token')
  window.location.href = '/auth'
}

function addAuthInterceptor(instance: ReturnType<typeof axios.create>) {
  // 请求拦截：自动注入 JWT
  instance.interceptors.request.use((config) => {
    const token = localStorage.getItem('access_token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  })

  // 响应拦截：401 时尝试刷新 token，失败则清除并跳转登录页
  // 登录/注册接口的 401 是凭证错误，不触发跳转
  instance.interceptors.response.use(
    (res) => res,
    async (error) => {
      const original = error.config
      if (error.response?.status === 401 && !original._retry) {
        const url = original.url || ''
        if (url.includes('/auth/login') || url.includes('/auth/register')) {
          return Promise.reject(error)
        }
        original._retry = true
        const newToken = await refreshAccessToken()
        if (newToken) {
          original.headers.Authorization = `Bearer ${newToken}`
          return instance(original)
        }
        // refresh 失败，清除并跳转
        forceLogout()
        return Promise.reject(error)
      }
      return Promise.reject(error)
    }
  )
}

addAuthInterceptor(api)
addAuthInterceptor(uploadApi)

export default api
export { uploadApi }
