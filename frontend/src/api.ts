import { getToken, notifyUnauthorized } from './auth'
import type {
  AuthUser,
  CustomerOrders,
  Decision,
  DecisionResponse,
  DemoInfo,
  LoginResponse,
  OperatorSessionSummary,
  OrderSummary,
  SessionView,
  TurnResponse,
} from './types'

const BASE = '/api/aftersales'

const ERROR_MESSAGES: Record<string, string> = {
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
  readonly status: number
  readonly code: string

  constructor(message: string, status = 0, code = 'network_error') {
    super(message)
    this.name = 'AftersalesApiError'
    this.status = status
    this.code = code
  }
}

type Method = 'GET' | 'POST'

// Every call carries the stored token. A 401 on an authenticated call means the
// token is missing, invalid or expired: it is dropped and the page goes back to
// the login view (stores/auth.ts). The login call itself is not authenticated.
async function call<T>(url: string, body?: unknown, method: Method = body === undefined ? 'GET' : 'POST',
  { authenticated = true } = {}): Promise<T> {
  const headers: Record<string, string> = {}
  const token = authenticated ? getToken() : ''
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let response: Response
  try {
    response = await fetch(url, {
      method,
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
  } catch {
    throw new AftersalesApiError('连接中断，暂时无法确认请求结果。')
  }
  const data: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = (data as { detail?: { code?: unknown } } | null)?.detail
    const code = typeof detail?.code === 'string' && detail.code ? detail.code : `http_${response.status}`
    if (response.status === 401 && authenticated) notifyUnauthorized()
    const fallback = response.status === 422
      ? '请求未通过校验，请检查输入（消息最多 2000 字）。'
      : '请求失败，请稍后重试或检查后端服务。'
    throw new AftersalesApiError(ERROR_MESSAGES[code] || fallback, response.status, code)
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    throw new AftersalesApiError('服务返回了无法读取的结果，请刷新会话状态。', response.status, 'invalid_response')
  }
  return data as T
}

const request = <T>(path: string, body?: unknown, method?: Method) => call<T>(`${BASE}${path}`, body, method)

const segment = (value: string) => encodeURIComponent(value)
const sessionPath = (id: string) => `/sessions/${segment(id)}`

export const aftersalesApi = {
  demo: () => request<DemoInfo>('/demo'),
  // Operator only.
  resetDemo: () => request<DemoInfo>('/demo/reset', undefined, 'POST'),
  // Customer only. No body: the server takes the customer from the token.
  createSession: () => request<SessionView>('/sessions', undefined, 'POST'),
  session: (sessionId: string) => request<SessionView>(sessionPath(sessionId)),
  sendMessage: (sessionId: string, text: string) =>
    request<TurnResponse>(`${sessionPath(sessionId)}/messages`, { text }),
  // Customer only: the token's own orders, lines and packages (read-only).
  orders: () => request<CustomerOrders>('/orders'),
  // Operator only.
  operatorSessions: () => request<{ items: OperatorSessionSummary[] }>('/operator/sessions'),
  operatorSession: (sessionId: string) => request<SessionView>(`/operator${sessionPath(sessionId)}`),
  // Operator only: the order and line one pending action of that session names (read-only).
  orderSummary: (sessionId: string, pendingActionId: string) =>
    request<OrderSummary>(`/operator${sessionPath(sessionId)}/actions/${segment(pendingActionId)}/order-summary`),
  decide: (sessionId: string, pendingActionId: string, decision: Decision) =>
    request<DecisionResponse>(`/operator${sessionPath(sessionId)}/decision`, {
      pending_action_id: pendingActionId,
      decision,
    }),
}

export type AftersalesApi = typeof aftersalesApi

export const authApi = {
  login: (username: string, password: string) =>
    call<LoginResponse>('/auth/login', { username, password }, 'POST', { authenticated: false }),
  me: () => call<AuthUser>('/auth/me'),
}
