// The shapes the /auth and /api/aftersales endpoints answer with (see
// docs/v2/m0-a1-aftersales-runtime.md). Display data only: no response ever
// carries a customer id, an idempotency key or model reasoning.

export type Role = 'customer' | 'operator'

export interface AuthUser {
  username: string
  role: Role
  display_name: string
  /** The server-side demo persona of a customer account; null for an operator. */
  persona_id: string | null
}

export interface LoginResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  user: AuthUser
}

export interface Persona {
  persona_id: string
  display_name: string
}

export interface DemoInfo {
  business_time: string
  personas: Persona[]
  operator: { approver_ref: string, notice: string }
  active_sessions: number
}

export type SessionStatus = 'OPEN' | 'NEEDS_CLARIFICATION' | 'WAITING_APPROVAL'
export type ActionStatus = 'EXECUTED' | 'WAITING_APPROVAL' | 'DENIED' | 'REJECTED' | 'STALE' | 'FAILED'
export type ActionName = 'create_return' | 'create_exchange' | 'escalate_to_human'

export interface Receipt {
  receipt_id: string
  resource_type: string
  resource_id: string
}

/** One action's persisted outcome plus the arguments it was submitted with. */
export interface ActionView {
  action_name: ActionName | string
  status: ActionStatus
  code?: string | null
  guard?: { decision: string, reason_code: string | null } | null
  pending_action_id: string | null
  receipt?: Receipt | null
  approval_recorded?: boolean
  idempotent_replay?: boolean
  decision_conflict?: boolean
  arguments: Record<string, string>
}

export interface Message {
  role: 'customer' | 'assistant'
  text: string
  kind?: string
  action_status?: ActionStatus
  pending_action_id?: string | null
}

export interface AuditEvent {
  event_seq: number
  event_name: string
  action_name: string | null
  pending_action_id: string | null
  receipt_id: string | null
  phase: string | null
  decision: string | null
  code: string | null
  approver_ref: string | null
  at: string
}

export interface Citation {
  ref: string
  producer: string
  source_type?: string
  locator?: string
  doc_id?: string
  title?: string
  version?: string
  section?: string
}

export interface Trace {
  steps: Record<string, unknown>[]
  model_calls: Record<string, unknown>[]
}

interface SessionHeader {
  session_id: string
  persona: Persona
  business_time: string
  status: SessionStatus
  /** The latest unresolved pending action, if any. */
  pending_action_id: string | null
}

export interface SessionView extends SessionHeader {
  messages: Message[]
  pending_actions: ActionView[]
  audit: AuditEvent[]
}

export interface TurnResponse extends SessionHeader {
  reply: { kind: string, text: string }
  clarification: { slots: string[] } | null
  citations: Citation[]
  action: ActionView | null
  trace: Trace | null
  audit: AuditEvent[]
}

export interface DecisionResponse extends TurnResponse {
  operator_decision: { pending_action_id: string, decision: Decision, approver_ref: string }
}

export type Decision = 'APPROVE' | 'REJECT'

export interface OperatorSessionSummary {
  session_id: string
  persona?: Persona
  status?: SessionStatus
  waiting_approval?: string[]
  message_count?: number
  /** Set instead of the rest when the session cannot be read right now. */
  error?: string
}

// -- order views (read-only displays) ----------------------------------------

/** Amounts are decimal strings as stored, e.g. "99.00". */
export interface OrderItem {
  order_item_id: string
  sku: string
  product_name: string
  category: string
  quantity: number
  /** 下单单价: one unit's price when the order was placed. */
  unit_price: string
  /** 商品小计: unit_price × quantity. */
  subtotal: string
}

/** A package belongs to the order (one order can ship in several), never to a line. */
export interface OrderPackage {
  tracking_no: string
  carrier: string
  status: string
  shipped_at: string
  delivered_at: string | null
  last_event_at: string
}

interface OrderHeader {
  order_id: string
  status: string
  paid_at: string | null
  /** 订单金额. */
  total_amount: string
  packages: OrderPackage[]
}

export interface Order extends OrderHeader {
  items: OrderItem[]
}

export interface CustomerOrders {
  business_time: string
  orders: Order[]
}

/** The order and the one line a pending action names; null when not the session customer's. */
export interface OrderSummary {
  session_id: string
  pending_action_id: string
  action_name: ActionName | string
  order: OrderHeader | null
  item: OrderItem | null
}
