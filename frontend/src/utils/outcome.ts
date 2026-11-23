// What a customer sees for one after-sales action: the status in plain words,
// the steps so far, and what they can do next. It follows the backend's
// fixed outcome wording (aftersales/action_outcome.py): EXECUTED means the
// return or exchange request was created and the after-sales case is now
// 待处理, not that the matter is finished.
import type { ActionStatus, ActionView, AuditEvent } from '@/types'

export type Tone = 'amber' | 'green' | 'red' | 'gray' | 'blue' | 'peach'

/** What the card offers next: re-read the session, open the 申请售后 sheet again, or draft a handoff request. */
export type NextKind = 'refresh' | 'reapply' | 'human'

export interface NextStep {
  kind: NextKind
  label: string
}

export type StepState = 'done' | 'now' | 'off' | 'bad'

export interface Step {
  title: string
  text: string
  state: StepState
}

export interface Outcome {
  /** 退货申请 / 换货申请 / 转人工申请 */
  title: string
  tag: string
  tone: Tone
  /** The status tag pulses while someone else still has to act. */
  pulse: boolean
  summary: string
  /** One sentence on what to do next; empty when nothing is needed. */
  hint: string
  steps: Step[]
  next: NextStep[]
  /** The short badge on the product row in the order list. */
  badge: string
}

/** A request of this session shown on its product line in the order list. */
export interface LineBadge {
  text: string
  tone: Tone
}

export const ACTION_LABELS: Record<string, string> = {
  create_return: '退货',
  create_exchange: '换货',
  escalate_to_human: '转人工',
}

const REFRESH: NextStep = { kind: 'refresh', label: '刷新进度' }
const REAPPLY: NextStep = { kind: 'reapply', label: '重新发起申请' }
const HUMAN: NextStep = { kind: 'human', label: '联系人工客服' }

interface DenyCopy {
  /** The backend's explanation (DENIED_EXPLANATIONS), without "申请没有提交". */
  text: string
  hint: string
  next: NextStep[]
  /** The explanation already says nothing was submitted (the backend's second tuple field). */
  saysNotSubmitted?: boolean
}

const ASK_HUMAN = '如需进一步处理，可以联系人工客服。'
const UNCLEAR: DenyCopy = { text: '订单或物流记录目前无法确认。', hint: '可以联系人工客服核实。', next: [HUMAN] }
const DUPLICATE: DenyCopy = { text: '这件商品已有处理中的售后申请，没有重复提交。', hint: '原申请会继续处理，不需要再次提交。', next: [], saysNotSubmitted: true }
const TARGET: DenyCopy = { text: '目标商品不能用于这次换货。', hint: '可以换其他规格，或改为退货。', next: [REAPPLY] }

/** Every Guard DENY reason code (aftersales/guard.py DENY_REASON_CODES). */
export const DENY_COPY: Record<string, DenyCopy> = {
  order_not_accessible: { text: '当前身份下未找到该订单。', hint: '请确认订单号后重新申请。', next: [REAPPLY] },
  order_item_not_in_order: { text: '该订单中没有找到这件商品。', hint: '请在订单里选中要处理的商品后重新申请。', next: [REAPPLY] },
  business_state_conflict: UNCLEAR,
  delivery_not_established: UNCLEAR,
  policy_conflict: UNCLEAR,
  not_delivered: { text: '订单尚未签收，暂时无法提交该申请。', hint: '包裹签收后可以再申请。', next: [] },
  order_status_ineligible: { text: '该订单当前状态不支持该申请。', hint: ASK_HUMAN, next: [HUMAN] },
  active_after_sales_case_exists: DUPLICATE,
  pending_request_exists: DUPLICATE,
  item_already_returned: { text: '这件商品已完成退货。', hint: ASK_HUMAN, next: [HUMAN] },
  handoff_required: { text: '该问题需要人工核实。', hint: '可以申请转人工处理。', next: [HUMAN] },
  non_returnable: { text: '该商品属于不支持退货的品类。', hint: ASK_HUMAN, next: [HUMAN] },
  no_applicable_policy: { text: '当前没有适用于该商品的售后规则。', hint: ASK_HUMAN, next: [HUMAN] },
  return_window_closed: { text: '该商品已超过退货时限。', hint: ASK_HUMAN, next: [HUMAN] },
  exchange_window_closed: { text: '该商品已超过换货时限。', hint: ASK_HUMAN, next: [HUMAN] },
  exchange_target_invalid: TARGET,
  exchange_target_incompatible: TARGET,
  inventory_unavailable: { text: '目标商品当前库存不足。', hint: '可以换其他规格，或改为退货。', next: [REAPPLY] },
  handoff_not_required: { text: '当前规则下该问题不需要转人工。', hint: '可以直接在这里继续咨询。', next: [] },
  handoff_ticket_exists: { text: '这件商品已有处理中的人工工单，没有重复创建。', hint: '客服会在原工单里跟进。', next: [], saysNotSubmitted: true },
}

const UNKNOWN_DENY: DenyCopy = { text: '这次申请不符合办理条件。', hint: ASK_HUMAN, next: [HUMAN] }

export function denyCopy(code: string | null | undefined): DenyCopy {
  return (code && DENY_COPY[code]) || UNKNOWN_DENY
}

/** The Guard's verdict in words, for the 处理依据 drawer. */
export function guardVerdict(action: Pick<ActionView, 'guard' | 'code' | 'status'> | null | undefined) {
  const decision = action?.guard?.decision
  if (decision === 'REQUIRE_APPROVAL') {
    return { tone: 'amber' as Tone, title: '符合条件，需要人工审核', text: '按售后规则，这类申请要由售后专员确认后才会执行。' }
  }
  if (decision === 'ALLOW') return { tone: 'green' as Tone, title: '符合条件', text: '按售后规则可以直接办理。' }
  if (decision === 'DENY' || action?.status === 'DENIED') {
    return { tone: 'red' as Tone, title: '不符合办理条件', text: denyCopy(action?.guard?.reason_code ?? action?.code).text }
  }
  return null
}

const needsApproval = (action: ActionView) => action.guard?.decision === 'REQUIRE_APPROVAL'
  || Boolean(action.approval_recorded)
  || (['WAITING_APPROVAL', 'REJECTED', 'STALE'] as ActionStatus[]).includes(action.status)

function resourceSentence(action: ActionView): string {
  const id = action.receipt?.resource_id
  if (action.action_name === 'escalate_to_human') return id ? `人工客服工单 ${id}，客服会跟进处理。` : '人工客服工单已创建，客服会跟进处理。'
  const base = id ? `售后单号 ${id}，当前状态：待处理。` : '售后单已生成，当前状态：待处理。'
  return action.action_name === 'create_exchange' ? `${base}换货申请提交后不会立即发货。` : base
}

export function describeAction(action: ActionView): Outcome {
  const label = ACTION_LABELS[action.action_name] ?? '售后'
  const title = `${label}申请`
  const isHandoff = action.action_name === 'escalate_to_human'
  const result = isHandoff ? '工单' : '售后单'
  const approval = needsApproval(action)
  const proposed: Step = { title: '提出申请', text: '智能客服根据你的描述提出申请', state: 'done' }
  const checked = (text: string, state: StepState = 'done'): Step => ({ title: '规则校验', text, state })
  const review = (text: string, state: StepState): Step => ({ title: '人工审核', text, state })

  switch (action.status) {
    case 'WAITING_APPROVAL':
      return {
        title, tag: '待审核', tone: 'amber', pulse: true, badge: '售后审核中',
        summary: '符合办理条件，需要售后专员审核；审核通过前不会执行。',
        hint: '页面不会自动刷新。审核完成后点「刷新进度」查看结果。',
        steps: [proposed, checked('符合条件，这类申请需人工审核'), review('售后专员审核中', 'now'),
          { title: isHandoff ? '创建工单' : '生成售后单', text: `审核通过后生成${result}`, state: 'off' }],
        next: [REFRESH],
      }
    case 'EXECUTED': {
      const tag = isHandoff ? '工单已创建 · 待跟进' : '申请已提交 · 待处理'
      const steps = [proposed, checked(approval ? '符合条件，这类申请需人工审核' : '符合条件')]
      if (approval) steps.push(review('售后专员已批准', 'done'))
      steps.push({ title: tag, text: isHandoff ? '人工客服工单已创建' : '售后单已生成，等待售后处理', state: 'done' })
      return {
        title, tag, tone: 'green', pulse: false, badge: isHandoff ? '人工处理中' : '售后单待处理',
        summary: resourceSentence(action),
        hint: isHandoff ? '' : '后续进度以售后单为准。',
        steps, next: [],
      }
    }
    case 'REJECTED':
      return {
        title, tag: '审核未通过', tone: 'red', pulse: false, badge: '审核未通过',
        summary: '申请没有通过人工审核，没有执行。',
        hint: ASK_HUMAN,
        steps: [proposed, checked('符合条件，这类申请需人工审核'), review('售后专员未批准', 'bad'),
          { title: '未执行', text: `没有生成${result}`, state: 'off' }],
        next: isHandoff ? [] : [HUMAN],
      }
    case 'DENIED': {
      const copy = denyCopy(action.guard?.reason_code ?? action.code)
      return {
        title, tag: '不符合办理条件', tone: 'red', pulse: false, badge: '不符合条件',
        summary: copy.saysNotSubmitted ? copy.text : `${copy.text}${isHandoff ? '工单没有创建。' : '申请没有提交。'}`,
        hint: copy.hint,
        steps: [proposed, checked(copy.text, 'bad'), { title: '未提交', text: `没有生成${result}`, state: 'off' }],
        next: copy.next.filter((step) => !(isHandoff && step.kind === 'human')),
      }
    }
    case 'STALE':
      return {
        title, tag: '已失效', tone: 'gray', pulse: false, badge: '申请已失效',
        summary: '审批期间订单或售后状态发生了变化，原申请没有执行。',
        hint: '请重新发起申请，会按最新状态重新核对。',
        steps: [proposed, checked('符合条件，这类申请需人工审核'), review('已审核', 'done'),
          { title: '未执行', text: '状态已变化，原申请失效', state: 'bad' }],
        next: [REAPPLY],
      }
    case 'FAILED':
    default:
      return {
        title, tag: '提交失败', tone: 'red', pulse: false, badge: '提交失败',
        summary: '系统暂时无法完成该操作，申请没有提交。',
        hint: '请稍后重新申请，或联系人工客服。',
        steps: [proposed, { title: '系统处理', text: '系统未能完成，申请没有提交', state: 'bad' }],
        next: isHandoff ? [REAPPLY] : [REAPPLY, HUMAN],
      }
  }
}

// A line can carry several requests of one session: a return that is still
// being reviewed, then a second try the Guard refused as a duplicate. The
// badge shows the request that is still open; the refusal stays in the chat.
const BADGE_RANK: Partial<Record<ActionStatus, number>> = { WAITING_APPROVAL: 2, EXECUTED: 1 }
const DUPLICATE_CODES = new Set(['active_after_sales_case_exists', 'pending_request_exists', 'handoff_ticket_exists'])
const ALREADY_OPEN: LineBadge = { text: '已有申请处理中', tone: 'amber' }

function lineBadge(action: ActionView): LineBadge {
  const code = action.guard?.reason_code ?? action.code
  if (action.status === 'DENIED' && code && DUPLICATE_CODES.has(code)) return ALREADY_OPEN
  const { badge, tone } = describeAction(action)
  return { text: badge, tone }
}

/**
 * One badge per product line, keyed by order then order item: a request waiting
 * for review outranks a submitted one, which outranks every closed outcome; among
 * equals the later request wins. `actions` is in the order they happened.
 */
export function lineBadges(actions: ActionView[]): Record<string, Record<string, LineBadge>> {
  const chosen = new Map<string, { orderId: string, itemId: string, action: ActionView }>()
  const rank = (action: ActionView) => BADGE_RANK[action.status] ?? 0
  for (const action of actions) {
    const orderId = action.arguments?.order_id
    const itemId = action.arguments?.order_item_id
    if (!orderId || !itemId) continue
    const key = `${orderId}/${itemId}`
    const held = chosen.get(key)
    if (!held || rank(action) >= rank(held.action)) chosen.set(key, { orderId, itemId, action })
  }
  const badges: Record<string, Record<string, LineBadge>> = {}
  for (const { orderId, itemId, action } of chosen.values()) {
    badges[orderId] ??= {}
    badges[orderId][itemId] = lineBadge(action)
  }
  return badges
}

/** Audit events in the customer's words (the 处理记录 tab). */
const EVENT_TITLES: Record<string, string> = {
  'guard.evaluated': '规则校验',
  'action.pending_created': '已创建待审核申请',
  'approval.recorded': '人工审核结果已记录',
  'resume.started': '开始执行已批准的申请',
  'resume.version_check': '重新核对订单和售后状态',
  'action.executed': '申请已执行',
  'action.not_executed': '申请没有执行',
  'action.replay_hit': '返回已有结果',
  'approval.conflict': '审核结果冲突',
}

const DECISION_WORDS: Record<string, string> = {
  REQUIRE_APPROVAL: '需人工审核',
  ALLOW: '符合条件',
  DENY: '不符合条件',
  APPROVE: '批准',
  REJECT: '拒绝',
  MATCH: '状态一致',
  MISMATCH: '状态已变化',
}

/**
 * The audit events of one action. The start-phase Guard event is written just
 * before the pending action exists, so it has no id: it belongs to the action
 * whose pending record comes right after it. An action that never got an id
 * (refused at once) cannot be told apart, so the whole session's trail is shown.
 */
export function actionEvents(audit: AuditEvent[], action: Pick<ActionView, 'pending_action_id' | 'action_name'> | null | undefined): AuditEvent[] {
  const id = action?.pending_action_id
  if (!id) return audit
  return audit.filter((event, index) => event.pending_action_id === id
    || (!event.pending_action_id && event.action_name === action.action_name && audit[index + 1]?.pending_action_id === id))
}

export function eventTitle(event: AuditEvent): string {
  const title = EVENT_TITLES[event.event_name] ?? event.event_name
  const decision = event.decision ? DECISION_WORDS[event.decision] : ''
  return decision ? `${title}：${decision}` : title
}
