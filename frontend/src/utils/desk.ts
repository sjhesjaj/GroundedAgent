// The approval desk's words: the session queue, the action a session opens
// on, what a decided action says, and the confirmation before a decision or a
// reset. The decision itself is only ever the server's answer to
// POST …/decision; nothing here predicts it.
import type { ActionView, Decision, DecisionResponse, OperatorSessionSummary, OrderSummary, SessionStatus } from '@/types'
import { ACTION_LABELS, denyCopy, type Tone } from './outcome'

export type QueueTab = 'waiting' | 'active' | 'all'

export const QUEUE_TABS: { key: QueueTab, label: string }[] = [
  { key: 'waiting', label: '待审批' },
  { key: 'active', label: '进行中' },
  { key: 'all', label: '全部' },
]

const waitingOf = (item: OperatorSessionSummary) => item.waiting_approval?.length ?? 0

/** 待审批: something to decide; 进行中: readable, nothing to decide; 全部: everything, waiting first. */
export function queueOf(items: OperatorSessionSummary[], tab: QueueTab): OperatorSessionSummary[] {
  if (tab === 'waiting') return items.filter((item) => waitingOf(item) > 0)
  if (tab === 'active') return items.filter((item) => !item.error && waitingOf(item) === 0)
  return [...items].sort((a, b) => waitingOf(b) - waitingOf(a))
}

export const SESSION_LABELS: Record<SessionStatus, { text: string, tone: Tone }> = {
  WAITING_APPROVAL: { text: '等待审批', tone: 'amber' },
  NEEDS_CLARIFICATION: { text: '等待补充信息', tone: 'peach' },
  OPEN: { text: '会话进行中', tone: 'gray' },
}

export function queueTag(item: OperatorSessionSummary): { text: string, tone: Tone, pulse: boolean } {
  if (item.error) return { text: '暂时无法读取', tone: 'red', pulse: false }
  const waiting = waitingOf(item)
  if (waiting > 0) return { text: `${waiting} 项待审批`, tone: 'amber', pulse: true }
  const label = item.status ? SESSION_LABELS[item.status] : undefined
  return { text: label?.text ?? item.status ?? '未知状态', tone: label?.tone ?? 'gray', pulse: false }
}

/** The action the 审批详情 column opens on: the first one still waiting, else the latest. */
export function focusAction(actions: ActionView[]): ActionView | null {
  return actions.find((action) => action.status === 'WAITING_APPROVAL') ?? actions[actions.length - 1] ?? null
}

/** The tag on an action in the desk (the operator's words, next to the customer's). */
export const ACTION_TAGS: Record<string, { text: string, tone: Tone }> = {
  WAITING_APPROVAL: { text: '待审批', tone: 'amber' },
  EXECUTED: { text: '申请已提交 · 待处理', tone: 'green' },
  REJECTED: { text: '已拒绝', tone: 'red' },
  DENIED: { text: '不符合条件', tone: 'red' },
  STALE: { text: '已失效', tone: 'gray' },
  FAILED: { text: '执行失败', tone: 'red' },
}

export function actionTag(action: ActionView) {
  if (action.status === 'EXECUTED' && action.action_name === 'escalate_to_human') return { text: '工单已创建 · 待跟进', tone: 'green' as Tone }
  return ACTION_TAGS[action.status] ?? { text: action.status, tone: 'gray' as Tone }
}

export const actionTitle = (action: Pick<ActionView, 'action_name'>) => `${ACTION_LABELS[action.action_name] ?? '售后'}申请`

/** Approved by a person, as opposed to allowed by the rules at once. */
const approved = (action: ActionView) => action.guard?.decision === 'REQUIRE_APPROVAL' || Boolean(action.approval_recorded)

export interface DeskResult {
  tone: Tone
  title: string
  text: string
  /** The execution receipt, on a line of its own. */
  receipt?: string
}

/** What the column's footer says once the action is no longer waiting. */
export function deskResult(action: ActionView): DeskResult | null {
  const handoff = action.action_name === 'escalate_to_human'
  switch (action.status) {
    case 'WAITING_APPROVAL':
      return null
    case 'EXECUTED': {
      const id = action.receipt?.resource_id
      const made = handoff ? (id ? `人工工单 ${id}` : '人工工单已创建') : (id ? `售后单 ${id} · 当前状态：待处理` : '售后单已生成 · 当前状态：待处理')
      const done = handoff ? '工单已创建' : '申请已提交'
      return {
        tone: 'green', title: approved(action) ? `已批准 · ${done}` : `按规则直接办理 · ${done}`, text: made,
        receipt: action.receipt?.receipt_id ? `回执 ${action.receipt.receipt_id}` : undefined,
      }
    }
    case 'REJECTED':
      return { tone: 'red', title: '已拒绝 · 未执行', text: '顾客会在申请进度里看到「审核未通过」。' }
    case 'STALE':
      return { tone: 'gray', title: '已批准，但状态已变化 · 未执行', text: '审批期间订单或售后状态发生了变化，原申请失效；顾客需要重新发起申请。' }
    case 'DENIED':
      return { tone: 'red', title: '不符合办理条件 · 未提交', text: denyCopy(action.guard?.reason_code ?? action.code).text }
    case 'FAILED':
    default:
      return { tone: 'red', title: '执行失败 · 未提交', text: '系统未能完成执行，申请没有提交。' }
  }
}

export type ToastKind = 'success' | 'warning' | 'error' | 'info'

/** The short note after POST …/decision answered (the server's outcome, never a guess). */
export function decisionToast(result: DecisionResponse): { kind: ToastKind, text: string } {
  const action = result.action
  if (action?.idempotent_replay) return { kind: 'info', text: '这笔申请已经审批过，结果没有变化。' }
  if (action?.decision_conflict) return { kind: 'warning', text: '这笔申请已有不同的审批结论，以已记录的结果为准。' }
  switch (action?.status) {
    case 'EXECUTED': {
      const id = action.receipt?.resource_id
      if (action.action_name === 'escalate_to_human') return { kind: 'success', text: id ? `已批准，人工工单 ${id} 已创建。` : '已批准，人工工单已创建。' }
      return { kind: 'success', text: id ? `已批准，售后单 ${id} 已创建（待处理）。` : '已批准，售后单已创建（待处理）。' }
    }
    case 'REJECTED':
      return { kind: 'success', text: '已拒绝这笔申请，未执行。' }
    case 'STALE':
      return { kind: 'warning', text: '已批准，但订单或售后状态已变化，申请没有执行。' }
    case 'FAILED':
      return { kind: 'error', text: '系统未能完成执行，申请没有提交。' }
    default:
      return { kind: 'info', text: result.reply?.text || '审批结果已记录。' }
  }
}

/**
 * GET …/order-summary for the action in review. `missing`: the server answered
 * but the order or the line is null; `recovering`: 409 recovery_pending. Neither
 * blocks a decision; both keep the transcript and the action on screen.
 */
export type SummaryState =
  | { state: 'idle' }
  | { state: 'loading' }
  | { state: 'ready', data: OrderSummary }
  | { state: 'missing', data: OrderSummary }
  | { state: 'recovering' }
  | { state: 'error', message: string }

export type Confirming = Decision | 'RESET'

export interface ConfirmCopy {
  title: string
  text: string
  confirm: string
  danger: boolean
}

export function confirmCopy(kind: Confirming, action?: Pick<ActionView, 'action_name'> | null): ConfirmCopy {
  const title = action ? actionTitle(action) : '申请'
  const result = action?.action_name === 'escalate_to_human' ? '创建人工工单' : '创建售后单（创建后为待处理）'
  if (kind === 'APPROVE') {
    return { title: `批准这笔${title}？`, text: `批准后，服务端会重新核对订单和售后状态，一致才会${result}。`, confirm: '确认批准', danger: false }
  }
  if (kind === 'REJECT') {
    return { title: `拒绝这笔${title}？`, text: '拒绝后申请结束，不会执行；顾客会在申请进度里看到「审核未通过」。这个决定不能撤回。', confirm: '确认拒绝', danger: true }
  }
  return { title: '重置演示数据？', text: '会清空所有演示会话和模拟售后记录，订单恢复到初始数据。这个操作不能撤回。', confirm: '确认重置', danger: true }
}
