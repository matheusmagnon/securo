import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { CalendarClock, ChevronDown, Link2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { formatCurrency } from '@/lib/format'
import type { NextSettlement, NextSettlementItem } from '@/types'

type Fmt = {
  locale: string
  dateLocale: string
  currency: string
}

function fmtDate(d: string, dateLocale: string) {
  return new Date(d + 'T00:00:00').toLocaleDateString(dateLocale, {
    day: '2-digit',
    month: '2-digit',
  })
}

/** Why the item counts when it does: bill due date, payment day, forecast. */
function referenceLabel(
  item: NextSettlementItem,
  t: (k: string, o?: Record<string, unknown>) => string,
  dateLocale: string,
) {
  const date = fmtDate(item.reference_date, dateLocale)
  switch (item.reference_kind) {
    case 'card_bill':
      return item.estimated
        ? t('splitGroups.refCardBillEstimated', { date })
        : t('splitGroups.refCardBill', { date })
    case 'card':
      return t('splitGroups.refCard', { date })
    case 'planned':
      return t('splitGroups.refPlanned', { date })
    case 'upcoming':
      return t('splitGroups.refUpcoming', { date })
    default:
      return t('splitGroups.refPaid', { date })
  }
}

/** "Yanna te deve R$ 49,95" / "Você deve R$ 448,72" from the viewer's side. */
function NetText({
  amount,
  counterpart,
  fmt,
  className = '',
}: {
  amount: number
  counterpart: string
  fmt: Fmt
  className?: string
}) {
  const { t } = useTranslation()
  if (!amount) return <span className={`text-muted-foreground ${className}`}>—</span>
  const value = formatCurrency(Math.abs(amount), fmt.currency, fmt.locale)
  return amount > 0 ? (
    <span className={`text-emerald-600 ${className}`}>
      {t('splitGroups.owesYou', { name: counterpart })} {value}
    </span>
  ) : (
    <span className={`text-rose-500 ${className}`}>
      {t('splitGroups.youOwe', { name: counterpart })} {value}
    </span>
  )
}

function ItemRow({
  item,
  fmt,
  memberName,
}: {
  item: NextSettlementItem
  fmt: Fmt
  memberName: (id: string) => string
}) {
  const { t } = useTranslation()
  const counterpart = item.net_by_member[0] ? memberName(item.net_by_member[0].member_id) : '—'
  return (
    <li className={`flex items-start justify-between gap-3 px-4 py-2.5 ${item.confirmed ? '' : 'opacity-70'}`}>
      <div className="min-w-0">
        <p className="text-sm font-medium text-foreground truncate">
          {item.description}
          {item.installment && !item.description.includes(item.installment)
            ? ` ${item.installment}`
            : ''}
          {!item.confirmed && (
            <span className="ml-2 align-middle text-[10px] uppercase tracking-wide rounded px-1.5 py-0.5 bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
              {t('splitGroups.plannedBadge')}
            </span>
          )}
        </p>
        <p className="text-xs text-muted-foreground">
          {referenceLabel(item, t, fmt.dateLocale)}
          {item.payer_name ? ` · ${t('splitGroups.paidBy', { name: item.payer_name })}` : ''}
          {' · '}
          {formatCurrency(Number(item.amount), item.currency, fmt.locale)}
        </p>
      </div>
      <NetText
        amount={Number(item.you_net)}
        counterpart={counterpart}
        fmt={fmt}
        className="text-sm font-semibold tabular-nums whitespace-nowrap text-right"
      />
    </li>
  )
}

export function NextSettlementCard({
  data,
  fmt,
  memberName,
  counterpartName,
  canRecord,
  onRecord,
  onLink,
}: {
  data: NextSettlement | undefined
  fmt: Fmt
  memberName: (id: string) => string
  /** The other member when the group is a pair; a generic label otherwise. */
  counterpartName: string
  canRecord: boolean
  onRecord: (items: NextSettlementItem[]) => void
  onLink: (settlementId: string) => void
}) {
  const { t } = useTranslation()
  const [showLater, setShowLater] = useState(false)
  if (!data) return null

  const next = data.items.filter((i) => i.bucket === 'next')
  const later = data.items.filter((i) => i.bucket === 'later')
  const unlinked = data.settlements.filter((s) => s.item_count === 0)
  const nextDate = data.next_settlement_date
    ? fmtDate(data.next_settlement_date, fmt.dateLocale)
    : null
  const confirmedNext = next.filter((i) => i.confirmed)

  return (
    <div className="bg-card rounded-xl border border-border shadow-sm overflow-hidden">
      <div className="px-4 sm:px-5 py-4 border-b border-border flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-foreground flex items-center gap-1.5">
            <CalendarClock size={14} className="text-muted-foreground" />
            {nextDate
              ? t('splitGroups.nextSettlementOn', { date: nextDate })
              : t('splitGroups.nextSettlement')}
          </p>
          <p className="text-xs text-muted-foreground mt-0.5">{t('splitGroups.nextSettlementHint')}</p>
        </div>
        {canRecord && confirmedNext.length > 0 && (
          <Button size="sm" variant="outline" className="h-8" onClick={() => onRecord(confirmedNext)}>
            {t('splitGroups.recordTheseItems')}
          </Button>
        )}
      </div>

      {unlinked.map((s) => (
        <div
          key={s.id}
          className="mx-4 mt-3 rounded-md border border-amber-300 bg-amber-50 dark:bg-amber-900/20 dark:border-amber-800 px-3 py-2 text-xs text-amber-900 dark:text-amber-200 flex flex-wrap items-center justify-between gap-2"
        >
          <span>
            {t('splitGroups.unlinkedSettlement', {
              date: fmtDate(s.date, fmt.dateLocale),
              amount: formatCurrency(Number(s.amount), fmt.currency, fmt.locale),
            })}
          </span>
          {canRecord && (
            <Button size="sm" variant="outline" className="h-7 gap-1 text-xs" onClick={() => onLink(s.id)}>
              <Link2 size={12} />
              {t('splitGroups.linkItems')}
            </Button>
          )}
        </div>
      ))}

      {data.items.length === 0 ? (
        <div className="text-center py-6 text-muted-foreground text-sm">{t('splitGroups.noOpenItems')}</div>
      ) : (
        <>
          {next.length > 0 && (
            <ul className="divide-y divide-border mt-1">
              {next.map((i) => (
                <ItemRow key={i.transaction_id} item={i} fmt={fmt} memberName={memberName} />
              ))}
            </ul>
          )}
          <div className="px-4 py-3 border-t border-border bg-muted/40 space-y-1 text-sm">
            <div className="flex justify-between gap-2">
              <span className="text-muted-foreground">{t('splitGroups.totalConfirmed')}</span>
              <NetText amount={Number(data.next_confirmed_total)} counterpart={counterpartName} fmt={fmt} className="font-semibold tabular-nums" />
            </div>
            {Number(data.next_total_with_planned) !== Number(data.next_confirmed_total) && (
              <div className="flex justify-between gap-2">
                <span className="text-muted-foreground">{t('splitGroups.totalWithPlanned')}</span>
                <NetText amount={Number(data.next_total_with_planned)} counterpart={counterpartName} fmt={fmt} className="tabular-nums" />
              </div>
            )}
          </div>
          {later.length > 0 && (
            <div className="border-t border-border">
              <button
                type="button"
                className="w-full flex items-center justify-between px-4 py-2.5 text-sm text-muted-foreground hover:bg-muted"
                onClick={() => setShowLater((v) => !v)}
                aria-expanded={showLater}
              >
                <span className="flex items-center gap-1.5">
                  <ChevronDown size={14} className={`transition-transform ${showLater ? 'rotate-180' : ''}`} />
                  {t('splitGroups.laterBucket', { count: later.length })}
                </span>
                <NetText amount={Number(data.later_total)} counterpart={counterpartName} fmt={fmt} className="tabular-nums" />
              </button>
              {showLater && (
                <ul className="divide-y divide-border">
                  {later.map((i) => (
                    <ItemRow key={i.transaction_id} item={i} fmt={fmt} memberName={memberName} />
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}

/** Checkbox list of open items, for recording or linking a settlement. */
export function SettlementItemPicker({
  items,
  selected,
  onToggle,
  fmt,
  memberName,
}: {
  items: NextSettlementItem[]
  selected: Set<string>
  onToggle: (id: string) => void
  fmt: Fmt
  memberName: (id: string) => string
}) {
  const { t } = useTranslation()
  return (
    <ul className="max-h-64 overflow-y-auto rounded-md border border-border divide-y divide-border">
      {items.map((i) => {
        const counterpart = i.net_by_member[0] ? memberName(i.net_by_member[0].member_id) : '—'
        return (
          <li key={i.transaction_id}>
            <label className="flex items-start gap-2 px-3 py-2 cursor-pointer hover:bg-muted">
              <input
                type="checkbox"
                className="mt-1"
                checked={selected.has(i.transaction_id)}
                onChange={() => onToggle(i.transaction_id)}
              />
              <span className="flex-1 min-w-0">
                <span className="block text-sm truncate">
                  {i.description}
                  {!i.confirmed ? ` (${t('splitGroups.plannedBadge')})` : ''}
                </span>
                <span className="block text-xs text-muted-foreground">
                  {referenceLabel(i, t, fmt.dateLocale)}
                </span>
              </span>
              <NetText amount={Number(i.you_net)} counterpart={counterpart} fmt={fmt} className="text-xs tabular-nums whitespace-nowrap" />
            </label>
          </li>
        )
      })}
    </ul>
  )
}
