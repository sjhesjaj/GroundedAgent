import { getToken, notifyUnauthorized } from './auth.js'

const BASE = '/api/aftersales'

const ERROR_MESSAGES = {
  session_not_found: '当前会话已失效，请新建会话。已显示的对话仍保留。',
  pending_action_not_found: '该待审批动作已不可用，请刷新会话状态。',
  llm_unavailable: '模型服务暂时不可用，请稍后重试。',
  agent_internal_error: 'Agent 处理失败，请稍后重试。',
  unknown_persona: '演示客户已不可用，请重新加载 Demo。',
  empty_message: '请输入消息后再发送。',
  conversation_full: '当前会话已达到消息上限，请新建会话。',
  too_many_sessions: '演示会话已达到上限，请重置 Demo。',
  decision_refused: '本次审批未被接受，请刷新会话后查看动作状态。',
  recovery_pending: '会话正在恢复，请稍后点「重新连接」。',
  policy_version_mismatch: '当前会话使用的策略与服务配置不一致，请新建会话后继续。',
  not_authenticated: '请先登录。',
  invalid_token: '登录状态无效，请重新登录。',
  token_expired: '登录已过期，请重新登录。',
  invalid_credentials: '用户名或密码错误。',
  forbidden: '当前账号无权执行该操作。',
}

export class AftersalesApiError extends Error {
  constructor(message, status = 0, code = 'network_error') {
    super(message)
    this.name = 'AftersalesApiError'
    this.status = status
    this.code = code
  }
}

// Every call carries the stored token. A 401 on an authenticated call means the
// token is missing, invalid or expired: it is dropped and the page goes back to
// the login view (Root.vue). The login call itself is not authenticated.
async function call(url, body, method = body === undefined ? 'GET' : 'POST', { authenticated = true } = {}) {
  const headers = {}
  const token = authenticated ? getToken() : ''
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let response
  try {
    response = await fetch(url, {
      method,
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
  } catch {
    throw new AftersalesApiError('连接中断，暂时无法确认请求结果。')
  }
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const code = data?.detail?.code || `http_${response.status}`
    if (response.status === 401 && authenticated) notifyUnauthorized()
    const fallback = response.status === 422
      ? '请求未通过校验，请检查输入（消息最多 2000 字）。'
      : '请求失败，请稍后重试或检查后端服务。'
    throw new AftersalesApiError(ERROR_MESSAGES[code] || fallback, response.status, code)
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new AftersalesApiError('服务返回了无法读取的结果，请刷新会话状态。', response.status, 'invalid_response')
  }
  return data
}

const request = (path, body, method) => call(`${BASE}${path}`, body, method)

const sessionPath = (id) => `/sessions/${encodeURIComponent(id)}`

export const aftersalesApi = {
  demo: () => request('/demo'),
  // Operator only.
  resetDemo: () => request('/demo/reset', undefined, 'POST'),
  // Customer only. No body: the server takes the customer from the token.
  createSession: () => request('/sessions', undefined, 'POST'),
  session: (sessionId) => request(sessionPath(sessionId)),
  sendMessage: (sessionId, text) => request(`${sessionPath(sessionId)}/messages`, { text }),
  // Operator only.
  operatorSessions: () => request('/operator/sessions'),
  operatorSession: (sessionId) => request(`/operator${sessionPath(sessionId)}`),
  decide: (sessionId, pendingActionId, decision) => request(`/operator${sessionPath(sessionId)}/decision`, {
    pending_action_id: pendingActionId,
    decision,
  }),
}

export const authApi = {
  login: (username, password) => call('/auth/login', { username, password }, 'POST', { authenticated: false }),
  me: () => call('/auth/me'),
}
