/**
 * Pure helpers for the Casa page: the bills to pay, what needs attention,
 * and the month's closing text a couple sends each other.
 */
import type { GroupOverview } from '@/types'

/** Today as YYYY-MM-DD in the viewer's local time. */
export function todayIso(now: Date = new Date()): string {
  const m = String(now.getMonth() + 1).padStart(2, '0')
  const d = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${m}-${d}`
}

export function daysBetween(fromIso: string, toIso: string): number {
  return Math.round((Date.parse(toIso + 'T00:00:00Z') - Date.parse(fromIso + 'T00:00:00Z')) / 86_400_000)
}

export interface PayBill {
  key: string
  description: string
  due_date: string
  amount: number
  payer_member_id: string | null
  paid: boolean
  paid_date: string | null
  number: number
  total: number | null
}

export function buildPayBills(overview: GroupOverview): PayBill[] {
  return overview.bills
    .map((b) => ({
      key: `${b.recurring_id}-${b.due_date}`,
      description: b.description,
      due_date: b.due_date,
      amount: Number(b.amount),
      payer_member_id: b.payer_member_id,
      paid: b.paid,
      paid_date: b.paid_date,
      number: b.number,
      total: b.total,
    }))
    .sort((a, b) => a.due_date.localeCompare(b.due_date) || a.description.localeCompare(b.description))
}

export type BillState = 'paid' | 'late' | 'open'

export function billState(bill: PayBill, today: string): BillState {
  if (bill.paid) return 'paid'
  return bill.due_date < today ? 'late' : 'open'
}

/** The first unpaid bill due today or later; else the oldest late one. */
export function nextBill(bills: PayBill[], today: string): PayBill | undefined {
  const open = bills.filter((b) => !b.paid)
  return open.find((b) => b.due_date >= today) ?? open[0]
}

/** Bills grouped by due day, in day order. */
export function billsByDay(bills: PayBill[]): [number, PayBill[]][] {
  const map = new Map<number, PayBill[]>()
  for (const b of bills) {
    const day = Number(b.due_date.slice(8, 10))
    map.set(day, [...(map.get(day) ?? []), b])
  }
  return [...map.entries()].sort((a, b) => a[0] - b[0])
}

export const SOON_DAYS = 5

export type CasaAlert =
  | { kind: 'late'; bill: PayBill; days: number }
  | { kind: 'soon'; bill: PayBill; days: number }
  | { kind: 'suggestions'; count: number }

/** Only what needs doing: late bills, bills due within SOON_DAYS, transfers to record. */
export function buildAlerts(bills: PayBill[], today: string, suggestionsCount: number): CasaAlert[] {
  const alerts: CasaAlert[] = []
  for (const bill of bills) {
    if (bill.paid) continue
    const days = daysBetween(today, bill.due_date)
    if (days < 0) alerts.push({ kind: 'late', bill, days: -days })
    else if (days <= SOON_DAYS) alerts.push({ kind: 'soon', bill, days })
  }
  if (suggestionsCount > 0) alerts.push({ kind: 'suggestions', count: suggestionsCount })
  return alerts
}

type T = (key: string, opts?: Record<string, unknown>) => string

/** The month's closing, as text to paste in a chat. */
export function buildClosingText(
  overview: GroupOverview,
  { t, money, fmtDate, monthName }: {
    t: T
    money: (v: number) => string
    fmtDate: (iso: string) => string
    monthName: string
  },
): string {
  const selfId = overview.self_member_id
  const names = new Map(overview.members.map((m) => [m.id, m.name]))
  const lines: string[] = [`*${t('casa.closing.heading', { month: monthName })}*`, '']
  // The other member first, the viewer last: the order people read it in.
  const ordered = [...overview.members].sort((a, b) => Number(a.id === selfId) - Number(b.id === selfId))
  for (const m of ordered) {
    const theirs = overview.items.filter((i) => i.payer_member_id === m.id)
    if (theirs.length === 0) continue
    lines.push(t('casa.closing.paidBy', { name: m.name }))
    for (const i of theirs) {
      const sign = i.type === 'credit' ? '−' : ''
      lines.push(`• ${i.description} (${fmtDate(i.date)}): ${sign}${money(Number(i.amount))}`)
    }
    const paid = Number(overview.paid_by.find((p) => p.member_id === m.id)?.amount ?? 0)
    lines.push(t('casa.closing.totalOf', { name: m.name, amount: money(paid) }), '')
  }
  lines.push(t('casa.closing.houseTotal', { amount: money(Number(overview.total)) }))
  for (const s of overview.share_of) {
    lines.push(t('casa.closing.shareOf', { name: names.get(s.member_id) ?? '—', amount: money(Number(s.amount)) }))
  }
  lines.push('')
  const line = overview.balance_lines[0]
  const amount = line ? Number(line.amount) : 0
  if (!line || amount === 0) {
    lines.push(t('casa.closing.settled'))
  } else {
    const other = names.get(line.member_id) ?? '—'
    const self = (selfId && names.get(selfId)) || '—'
    const [from, to] = amount < 0 ? [self, other] : [other, self]
    lines.push(t('casa.closing.pending', { from, to, amount: money(Math.abs(amount)) }))
  }
  return lines.join('\n')
}

/** Each member's share of `total` in proportion to their income. */
export function proportionalShares(total: number, incomeSelf: number, incomeOther: number) {
  const sum = incomeSelf + incomeOther
  const pctSelf = sum > 0 ? (incomeSelf / sum) * 100 : 50
  return { pctSelf, shareSelf: (total * pctSelf) / 100, shareOther: total - (total * pctSelf) / 100 }
}
