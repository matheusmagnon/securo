import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, Home, Scale } from 'lucide-react'

import { groups as groupsApi } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { currentMonth } from '@/lib/month-utils'
import { useDisplayLocale, useDateLocale } from '@/hooks/use-display-locale'
import { MonthStepper } from '@/components/month-stepper'
import { PageHeader } from '@/components/page-header'
import { CategoryIcon } from '@/components/category-icon'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import type { GroupOverview, GroupOverviewItem } from '@/types'
import {
  CasaAlerts,
  ClosingButton,
  HistoryCard,
  IncomeSplitSimulator,
  PayablesCard,
} from '@/components/casa-cards'
import { buildPayBills } from '@/lib/casa'

const STORAGE_KEY = 'casa.groupId'

// The viewer is always blue, the other member pink, as in the reference.
const SELF_COLOR = 'bg-blue-600'
const OTHER_COLOR = 'bg-pink-500'

function readStoredGroup(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

function storeGroup(id: string) {
  try {
    localStorage.setItem(STORAGE_KEY, id)
  } catch {
    /* per-viewer convenience only */
  }
}

function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <section className={cn('bg-card rounded-2xl border border-border shadow-sm p-5', className)}>
      {children}
    </section>
  )
}

function CardTitle({ title, hint, action }: { title: string; hint?: string; action?: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 mb-4">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-foreground">{title}</h2>
        {hint && <p className="text-xs text-muted-foreground mt-0.5">{hint}</p>}
      </div>
      {action}
    </div>
  )
}

/** "R$ 1.234" big, ",56" small: the reference's headline style. */
function BigMoney({ value, currency, locale, className }: { value: number; currency: string; locale: string; className?: string }) {
  const text = formatCurrency(value, currency, locale)
  const match = text.match(/^(.*?)([.,]\d{2})(\D*)$/)
  if (!match) return <span className={className}>{text}</span>
  return (
    <span className={cn('font-semibold tracking-tight tabular-nums', className)}>
      {match[1]}
      <span className="text-[0.55em] text-muted-foreground font-medium">{match[2]}</span>
      {match[3]}
    </span>
  )
}

function StatusBadge({ item }: { item: GroupOverviewItem }) {
  const { t } = useTranslation()
  const styles: Record<string, string> = {
    planned: 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-900',
    pending: 'bg-sky-50 text-sky-700 border-sky-200 dark:bg-sky-950/40 dark:text-sky-300 dark:border-sky-900',
    upcoming: 'bg-muted text-muted-foreground border-border',
    paid: 'bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-900',
  }
  return (
    <span className={cn('text-[10px] font-semibold px-1.5 py-0.5 rounded-full border whitespace-nowrap', styles[item.status] ?? styles.upcoming)}>
      {t(`casa.status.${item.status}`, { defaultValue: item.status })}
    </span>
  )
}

export default function CasaPage() {
  const { t } = useTranslation()
  const locale = useDisplayLocale()
  const dateLocale = useDateLocale()
  const [month, setMonth] = useState(currentMonth)
  const [chosenGroupId, setGroupId] = useState<string | null>(readStoredGroup)

  const { data: groupList, isLoading: loadingGroups } = useQuery({
    queryKey: ['groups', 'active-list'],
    queryFn: () => groupsApi.list(false),
  })

  // The remembered group while it is still listed, else the first active one.
  const groupId =
    groupList?.some((g) => g.id === chosenGroupId) ? chosenGroupId : (groupList?.[0]?.id ?? null)

  const { data: overview, isLoading } = useQuery({
    queryKey: ['groups', groupId, 'overview', month],
    queryFn: () => groupsApi.overview(groupId!, month),
    enabled: !!groupId,
  })

  const { data: suggestions = [] } = useQuery({
    queryKey: ['groups', groupId, 'settlement-suggestions'],
    queryFn: () => groupsApi.settlementSuggestions.list(groupId!),
    enabled: !!groupId,
  })

  const fmtDate = (iso: string, opts: Intl.DateTimeFormatOptions = { day: '2-digit', month: '2-digit' }) =>
    new Date(iso + 'T00:00:00').toLocaleDateString(dateLocale, opts)

  const group = groupList?.find((g) => g.id === groupId)

  if (!loadingGroups && (groupList?.length ?? 0) === 0) {
    return (
      <div className="max-w-[1600px] mx-auto">
        <PageHeader section={t('casa.section')} title={t('casa.title')} />
        <Card>
          <p className="text-sm text-muted-foreground">{t('casa.noGroup')}</p>
          <Link to="/groups" className="text-sm text-primary font-medium mt-2 inline-flex items-center gap-1">
            {t('casa.goToGroups')} <ArrowRight size={14} />
          </Link>
        </Card>
      </div>
    )
  }

  return (
    <div className="max-w-[1600px] mx-auto">
      <PageHeader
        section={group ? group.name : t('casa.section')}
        title={t('casa.title')}
        action={
          <div className="flex items-center gap-2">
            {groupList && groupList.length > 1 && (
              <select
                value={groupId ?? ''}
                onChange={(e) => {
                  setGroupId(e.target.value)
                  storeGroup(e.target.value)
                }}
                className="h-8 rounded-lg border border-border bg-card px-2 text-sm"
                aria-label={t('casa.chooseGroup')}
              >
                {groupList.map((g) => (
                  <option key={g.id} value={g.id}>{g.name}</option>
                ))}
              </select>
            )}
            <MonthStepper
              value={month}
              onChange={setMonth}
              locale={locale}
              prevLabel={t('casa.prevMonth')}
              nextLabel={t('casa.nextMonth')}
            />
          </div>
        }
      />

      {isLoading || !overview ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <Skeleton className="h-48 rounded-2xl" />
          <Skeleton className="h-48 rounded-2xl" />
          <Skeleton className="h-80 md:col-span-2 rounded-2xl" />
          <Skeleton className="h-80 rounded-2xl" />
        </div>
      ) : (
        <CasaBody
          overview={overview}
          groupId={groupId!}
          suggestionsCount={suggestions.length}
          locale={locale}
          fmtDate={fmtDate}
          month={month}
        />
      )}
    </div>
  )
}

function CasaBody({
  overview,
  groupId,
  suggestionsCount,
  locale,
  fmtDate,
  month,
}: {
  overview: GroupOverview
  groupId: string
  suggestionsCount: number
  locale: string
  fmtDate: (iso: string, opts?: Intl.DateTimeFormatOptions) => string
  month: string
}) {
  const { t } = useTranslation()
  const currency = overview.default_currency
  const money = (v: number | string) => formatCurrency(Number(v), currency, locale)
  const selfId = overview.self_member_id
  const nameOf = useMemo(() => {
    const names = new Map(overview.members.map((m) => [m.id, m.name]))
    return (id: string | null) => (id && names.get(id)) || '—'
  }, [overview.members])
  const other = overview.members.find((m) => m.id !== selfId)
  const colorOf = (id: string | null) => (id === selfId ? SELF_COLOR : OTHER_COLOR)
  const isSelf = (id: string | null) => id === selfId

  // Balance with the other member (couple: one line).
  const line = overview.balance_lines[0]
  const owed = line ? Number(line.amount) : 0
  const upcoming = overview.upcoming_lines[0]
  const upcomingAmount = upcoming ? Number(upcoming.amount) : 0

  const total = Number(overview.total)
  const paidSelf = Number(overview.paid_by.find((p) => p.member_id === selfId)?.amount ?? 0)
  const paidOther = Number(overview.paid_by.find((p) => p.member_id !== selfId)?.amount ?? 0)
  const paidSum = paidSelf + paidOther
  const selfPct = paidSum > 0 ? Math.round((paidSelf / paidSum) * 1000) / 10 : 50
  const shareSelf = Number(overview.share_of.find((p) => p.member_id === selfId)?.amount ?? 0)

  const items = [...overview.items].reverse()
  const payBills = useMemo(() => buildPayBills(overview), [overview])
  const monthName = new Date(month + '-01T00:00:00').toLocaleDateString(locale, { month: 'long', year: 'numeric' })
  const selfName = overview.members.find((m) => m.id === selfId)?.name ?? t('casa.you')

  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      <CasaAlerts
        bills={payBills}
        groupId={groupId}
        suggestionsCount={suggestionsCount}
        money={money}
        fmtDate={fmtDate}
      />
      {/* Settle-up */}
      <Card className="flex flex-col">
        <CardTitle
          title={t('casa.settle.title')}
          hint={t('casa.settle.hint')}
          action={<Scale size={28} className="text-muted-foreground/60" />}
        />
        {owed === 0 ? (
          <p className="text-2xl font-semibold text-emerald-600">{t('casa.settle.allSettled')}</p>
        ) : (
          <div>
            <p className="text-sm text-muted-foreground">
              {owed < 0
                ? t('casa.settle.youOwe', { name: nameOf(line.member_id) })
                : t('casa.settle.owesYou', { name: nameOf(line.member_id) })}
            </p>
            <BigMoney
              value={Math.abs(owed)}
              currency={currency}
              locale={locale}
              className={cn('text-4xl sm:text-5xl', owed < 0 ? 'text-rose-500' : 'text-emerald-600')}
            />
          </div>
        )}
        {upcomingAmount !== 0 && upcoming && (
          <p className="text-sm text-muted-foreground mt-3">
            {upcomingAmount > 0
              ? t('casa.settle.upcomingOwesYou', { name: nameOf(upcoming.member_id), amount: money(Math.abs(upcomingAmount)) })
              : t('casa.settle.upcomingYouOwe', { name: nameOf(upcoming.member_id), amount: money(Math.abs(upcomingAmount)) })}
          </p>
        )}
        <dl className="grid grid-cols-3 gap-3 mt-5 mb-4">
          {[
            { label: t('casa.stats.total'), value: money(total) },
            { label: t('casa.stats.share'), value: money(shareSelf) },
            { label: t('casa.stats.open'), value: money(payBills.filter((b) => !b.paid).reduce((s, b) => s + b.amount, 0)) },
          ].map((s) => (
            <div key={s.label} className="rounded-xl bg-muted/60 px-3 py-2.5 min-w-0">
              <dt className="text-[11px] text-muted-foreground truncate">{s.label}</dt>
              <dd className="text-sm font-semibold tabular-nums truncate">{s.value}</dd>
            </div>
          ))}
        </dl>
        <div className="flex flex-wrap items-center gap-2 mt-auto pt-4 border-t border-border">
          <ClosingButton overview={overview} money={money} fmtDate={fmtDate} monthName={monthName} />
          <Link
            to={`/groups/${groupId}`}
            className="ml-auto inline-flex items-center gap-1 text-sm font-medium text-primary"
          >
            {t('casa.settle.openGroup')} <ArrowRight size={14} />
          </Link>
        </div>
      </Card>

      {/* Bills of the month */}
      <PayablesCard bills={payBills} selfId={selfId} nameOf={nameOf} money={money} fmtDate={fmtDate} />

      {/* Contracts */}
      <Card>
        <CardTitle
          title={t('casa.contracts.title')}
          hint={t('casa.contracts.hint')}
          action={<Home size={26} className="text-muted-foreground/60" />}
        />
        {overview.contracts.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t('casa.contracts.none')}</p>
        ) : (
          <ul className="space-y-5">
            {overview.contracts.map((c) => {
              const pct = c.total > 0 ? Math.min(100, (c.done / c.total) * 100) : 0
              return (
                <li key={c.recurring_id}>
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-sm font-medium truncate">{c.description}</span>
                    <span className="text-xs text-muted-foreground tabular-nums whitespace-nowrap">
                      {t('casa.installment', { n: c.done, total: c.total })}
                    </span>
                  </div>
                  <div className="mt-1.5 h-2 rounded-full bg-muted overflow-hidden">
                    <div className="h-full bg-emerald-500 rounded-full" style={{ width: `${pct}%` }} />
                  </div>
                  <div className="mt-1.5 flex flex-wrap justify-between gap-x-2 text-xs text-muted-foreground">
                    <span>
                      {t('casa.contracts.remaining', { amount: money(c.remaining_amount) })}
                    </span>
                    <span>
                      {t('casa.contracts.ends', { date: fmtDate(c.end_date, { month: 'short', year: 'numeric' }) })}
                    </span>
                  </div>
                  <div className="mt-0.5 text-xs text-muted-foreground inline-flex items-center gap-1">
                    <span className={cn('h-1.5 w-1.5 rounded-full', colorOf(c.payer_member_id))} />
                    {t('casa.contracts.each', { amount: money(c.amount), freq: t(`casa.freq.${c.frequency}`, { defaultValue: c.frequency }) })}
                    {' · '}
                    {isSelf(c.payer_member_id) ? t('casa.you') : nameOf(c.payer_member_id)}
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </Card>

      {/* Division of the month */}
      <Card className="md:col-span-2 xl:row-span-2">
        <CardTitle title={t('casa.division.title')} hint={t('casa.division.hint')} />
        <p className="text-xs text-muted-foreground">{t('casa.division.total')}</p>
        <BigMoney value={total} currency={currency} locale={locale} className="text-3xl" />

        <div className="mt-4">
          <div className="flex justify-between text-xs text-muted-foreground mb-1">
            <span className="inline-flex items-center gap-1.5">
              <span className={cn('h-2 w-2 rounded-full', SELF_COLOR)} /> {t('casa.division.youPaid')}
            </span>
            <span className="inline-flex items-center gap-1.5">
              {t('casa.division.otherPaid', { name: other?.name ?? '—' })} <span className={cn('h-2 w-2 rounded-full', OTHER_COLOR)} />
            </span>
          </div>
          <div className="flex justify-between text-sm font-semibold tabular-nums mb-1.5">
            <span>{money(paidSelf)}</span>
            <span>{money(paidOther)}</span>
          </div>
          <div className="relative flex h-6 overflow-hidden rounded-lg text-[11px] font-semibold text-white">
            <div className={cn(SELF_COLOR, 'flex items-center px-2')} style={{ width: `${selfPct}%` }}>
              {selfPct >= 12 && `${selfPct.toLocaleString(locale)}%`}
            </div>
            <div className={cn(OTHER_COLOR, 'flex flex-1 items-center justify-end px-2')}>
              {100 - selfPct >= 12 && `${(Math.round((100 - selfPct) * 10) / 10).toLocaleString(locale)}%`}
            </div>
            {/* Where an even split would be. */}
            <div className="absolute inset-y-0 left-1/2 w-0.5 bg-foreground/70" />
          </div>
          <p className="text-xs text-muted-foreground mt-2">
            {t('casa.division.yourShare', { amount: money(shareSelf) })}
          </p>
        </div>

        <h3 className="text-xs font-semibold text-foreground mt-6 mb-2">{t('casa.division.whoPaid')}</h3>
        {items.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t('casa.division.none')}</p>
        ) : (
          <ul className="divide-y divide-border">
            {items.map((item) => (
              <li key={item.transaction_id} className="flex items-center gap-3 py-2.5">
                <span className={cn('h-2 w-2 rounded-full shrink-0', colorOf(item.payer_member_id))} />
                <CategoryIcon icon={item.category_icon} color={item.category_color} size="sm" />
                <div className="min-w-0 flex-1">
                  <div className="text-sm truncate">{item.description}</div>
                  <div className="text-xs text-muted-foreground">
                    {fmtDate(item.date)}
                    {item.category_name ? ` · ${item.category_name}` : ''}
                    {' · '}
                    {isSelf(item.payer_member_id) ? t('casa.youPaidShort') : t('casa.otherPaidShort', { name: nameOf(item.payer_member_id) })}
                  </div>
                </div>
                <StatusBadge item={item} />
                <div className="text-right">
                  <div className={cn('text-sm tabular-nums font-medium', item.type === 'credit' && 'text-emerald-600')}>
                    {money(item.amount)}
                  </div>
                  {item.my_share != null && (
                    <div className="text-[11px] text-muted-foreground tabular-nums">
                      {t('casa.yourPart', { amount: money(item.my_share) })}
                    </div>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
        <IncomeSplitSimulator
          total={total}
          selfName={selfName}
          otherName={other?.name ?? '—'}
          money={money}
          locale={locale}
        />
      </Card>


      <HistoryCard groupId={groupId} month={month} locale={locale} money={money} otherName={other?.name ?? '—'} />
    </div>
  )
}
