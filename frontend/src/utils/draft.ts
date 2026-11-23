// The 申请售后 sheet only writes a sentence for the composer. The customer reads
// it and presses 发送 themselves; the agent then reads the order, proposes the
// action and the Guard decides, exactly as for a typed message. Nothing here
// fills in what the customer did not choose: an exchange without a target spec
// is sent without one, and the agent asks for it (a clarification turn).

import type { OrderItem } from '@/types'

export type ServiceKind = 'return' | 'exchange' | 'human'

/** A product card on a customer message: shown with it, never sent (the text names the product). */
export interface Attachment {
  order_id: string
  item: OrderItem
}

/** The backend's customer reason codes (aftersales/arguments.py), in the words the customer picks. */
export type ReasonCode = 'no_longer_wanted' | 'size_or_spec_mismatch' | 'quality_issue'

export interface ServiceOption {
  kind: ServiceKind
  label: string
  description: string
}

export const SERVICE_OPTIONS: ServiceOption[] = [
  { kind: 'return', label: '退货', description: '寄回商品，审核通过后生成售后单' },
  { kind: 'exchange', label: '换货', description: '换同款的其他规格，需要有库存' },
  { kind: 'human', label: '人工协助', description: '转售后专员处理' },
]

export const REASON_LABELS: Record<ReasonCode, string> = {
  no_longer_wanted: '不想要了',
  size_or_spec_mismatch: '尺码或规格不合适',
  quality_issue: '质量问题',
}

/** Return accepts all three reasons; exchange has no "不想要了"; a handoff takes none. */
export const REASONS_FOR: Record<ServiceKind, ReasonCode[]> = {
  return: ['no_longer_wanted', 'size_or_spec_mismatch', 'quality_issue'],
  exchange: ['size_or_spec_mismatch', 'quality_issue'],
  human: [],
}

export interface DraftTarget {
  order_id: string
  product_name: string
}

function sentence(text: string): string {
  const trimmed = text.trim()
  if (!trimmed) return ''
  return /[。！？.!?]$/.test(trimmed) ? trimmed : `${trimmed}。`
}

/** The sentence put into the composer. `note` is the customer's own words, passed through as written. */
export function composeDraft(kind: ServiceKind, target: DraftTarget, reason: ReasonCode | null, note = ''): string {
  const subject = `订单 ${target.order_id} 的「${target.product_name}」`
  const why = reason ? `，原因：${REASON_LABELS[reason]}。` : '。'
  const extra = note.trim()
  if (kind === 'return') return `我要退货：${subject}${why}${sentence(extra)}`
  if (kind === 'exchange') return `我想换货：${subject}${why}${extra ? sentence(`想换成：${extra}`) : ''}`
  return `我需要人工协助：${subject}。${sentence(extra)}`
}

/** The start of a question about one product (咨询), finished by the customer. */
export function askDraft(target: DraftTarget): string {
  return `关于订单 ${target.order_id} 的「${target.product_name}」：`
}
