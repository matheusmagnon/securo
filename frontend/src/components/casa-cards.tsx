/**
 * Cards of the Casa page that read more than the month's overview:
 * alerts, bills to pay, the closing text, the income simulator, the
 * history chart, and the compact card on the dashboard.
 */
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { useQueries, useQuery } from '@tanstack/react-query'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import {
  AlertTriangle,
  ArrowRight,
  CalendarClock,
  CheckCircle2,
  ChevronDown,
  Circle,
  ClipboardCopy,
  FileText,
  HeartHandshake,
  Lightbulb,
} from 'lucide-react'

import { groups as groupsApi } from '@/lib/api'
import { formatCurrency } from '@/lib/format'
import { currentMonth, shiftMonth } from '@/lib/month-utils'
import {
  billState,
  billsByDay,
  buildAlerts,
  buildClosingText,
  buildPayBills,
  daysBetween,
  nextBill,
  proportionalShares,
  todayIso,
  type PayBill,
} from '@/lib/casa'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { GroupOverview } from '@/types'

// The viewer is always blue, the other member pink.
const SELF_HEX = '#2563eb'
const OTHER_HEX = '#ec4899'
const SELF_COLOR = 'bg-blue-600'
const OTHER_COLOR = 'bg-pink-500'

type FmtDate = (iso: string, opts?: Intl.DateTimeFormatOptions) => string
type Money = (v: number) => string

function Card({ className, children }: { className?: string; children: React.ReactNode }) {
  return (
    <section className={cn('bg-card rounded-2xl border border-border shadow-sm p-5', className)}>
      {children}
    </section>
  )
}

function CardTitle({ title, hint, action }: { title: React.ReactNode; hint?: React.ReactNode; action?: React.ReactNode }) {
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

// ---------------------------------------------------------------------------
// Alerts: thin banners, only when something needs doing
// ---------------------------------------------------------------------------

export function CasaAlerts({
  bills,
  groupId,
  suggestionsCount,
  money,
  fmtDate,
}: {
  bills: PayBill[]
  groupId: string
  suggestionsCount: number
  money: Money
  fmtDate: FmtDate
}) {
  const { t } = useTranslation()
  const alerts = buildAlerts(bills, todayIso(), suggestionsCount)
  if (alerts.length === 0) return null

  return (
    <ul className="md:col-span-2 xl:col-span-3 flex flex-col gap-2">
      {alerts.map((a) => {
        let icon: React.ReactNode
        let tone: string
        let text: string
        let to: string | undefined
        if (a.kind === 'late') {
          icon = <AlertTriangle size={16} />
          tone = 'text-rose-700 bg-rose-50 dark:bg-rose-950/40 dark:text-rose-300'
          text = t('casa.alerts.late', { name: a.bill.description, date: fmtDate(a.bill.due_date), amount: money(a.bill.amount) })
        } else if (a.kind === 'soon') {
          icon = <CalendarClock size={16} />
          tone = 'text-amber-700 bg-amber-50 dark:bg-amber-950/40 dark:text-amber-300'
          const opts = { name: a.bill.description, amount: money(a.bill.amount), count: a.days }
          text = a.days === 0
            ? t('casa.alerts.dueToday', opts)
            : a.days === 1
              ? t('casa.alerts.dueTomorrow', opts)
              : t('casa.alerts.dueIn', opts)
        } else {
          icon = <Lightbulb size={16} />
          tone = 'text-amber-700 bg-amber-50 dark:bg-amber-950/40 dark:text-amber-300'
          text = t('casa.settle.suggestions', { count: a.count })
          to = `/groups/${groupId}`
        }
        const body = (
          <div className={cn('flex items-center gap-2.5 rounded-xl px-3.5 py-2 text-sm', tone)}>
            <span className="shrink-0">{icon}</span>
            <span className="flex-1 text-foreground/90">{text}</span>
            {to && <ArrowRight size={14} className="shrink-0" />}
          </div>
        )
        const key = a.kind === 'suggestions' ? 'suggestions' : `${a.kind}-${a.bill.key}`
        return <li key={key}>{to ? <Link to={to}>{body}</Link> : body}</li>
      })}
    </ul>
  )
}

// ---------------------------------------------------------------------------
// Bills to pay
// ---------------------------------------------------------------------------

export function PayablesCard({
  bills,
  selfId,
  nameOf,
  money,
  fmtDate,
}: {
  bills: PayBill[]
  selfId: string | null
  nameOf: (id: string | null) => string
  money: Money
  fmtDate: FmtDate
}) {
  const { t } = useTranslation()
  const today = todayIso()
  const [day, setDay] = useState<number | null>(null)
  const days = useMemo(() => billsByDay(bills), [bills])

  const open = bills.filter((b) => !b.paid)
  const next = nextBill(bills, today)
  const nextDay = next ? Number(next.due_date.slice(8, 10)) : null
  const shown = day == null ? bills : bills.filter((b) => Number(b.due_date.slice(8, 10)) === day)
  const openTotal = open.reduce((s, b) => s + b.amount, 0)

  const dueText = (bill: PayBill) => {
    const d = daysBetween(today, bill.due_date)
    if (d < 0) return <span className="text-rose-600 font-medium">{t('casa.payables.lateFor', { count: -d })}</span>
    if (d === 0) return t('casa.payables.dueToday')
    return t('casa.payables.dueIn', { count: d })
  }

  return (
    <Card>
      <CardTitle
        title={t('casa.payables.title')}
        hint={
          bills.length > 0
            ? t('casa.payables.summary', { paid: bills.length - open.length, total: bills.length, amount: money(openTotal) })
            : undefined
        }
      />
      {bills.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t('casa.bills.none')}</p>
      ) : (
        <>
          {next ? (
            <div className="rounded-xl bg-muted/60 px-3 py-2.5 mb-3">
              <p className="text-[11px] text-muted-foreground">{t('casa.payables.next')}</p>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-sm font-semibold truncate">
                  {t('casa.payables.nextLine', { day: nextDay, name: next.description })}
                </span>
                <span className="text-sm font-semibold tabular-nums">{money(next.amount)}</span>
              </div>
              <p className="text-xs text-muted-foreground">{dueText(next)}</p>
            </div>
          ) : (
            <p className="rounded-xl bg-emerald-50 dark:bg-emerald-950/40 text-emerald-700 dark:text-emerald-300 px-3 py-2.5 mb-3 text-sm font-medium">
              {t('casa.payables.allPaid')}
            </p>
          )}

          <div className="flex gap-1.5 overflow-x-auto pb-1 mb-3 -mx-1 px-1">
            <button
              type="button"
              onClick={() => setDay(null)}
              className={cn(
                'shrink-0 rounded-lg border px-2.5 py-1 text-xs font-medium',
                day == null ? 'bg-foreground text-background border-foreground' : 'border-border text-muted-foreground',
              )}
            >
              {t('casa.payables.all')}
            </button>
            {days.map(([d, list]) => {
              const allPaid = list.every((b) => b.paid)
              const late = list.some((b) => billState(b, today) === 'late')
              const isNext = d === nextDay
              const label = allPaid
                ? t('casa.payables.chip.paid')
                : late
                  ? t('casa.payables.chip.late')
                  : isNext
                    ? t('casa.payables.next')
                    : t('casa.payables.chip.open')
              return (
                <button
                  key={d}
                  type="button"
                  onClick={() => setDay(day === d ? null : d)}
                  title={label}
                  aria-label={`${d} · ${label}`}
                  aria-pressed={day === d}
                  className={cn(
                    'shrink-0 rounded-lg border px-2.5 py-1 text-xs font-semibold tabular-nums flex flex-col items-center min-w-[3rem] leading-tight',
                    day === d
                      ? 'bg-foreground text-background border-foreground'
                      : late
                        ? 'bg-rose-50 border-rose-200 text-rose-700 dark:bg-rose-950/40 dark:border-rose-900 dark:text-rose-300'
                        : allPaid
                          ? 'border-border text-muted-foreground'
                          : isNext
                            ? 'bg-primary/10 border-primary/30 text-primary'
                            : 'border-border',
                  )}
                >
                  <span className={cn(allPaid && day !== d && 'line-through decoration-1')}>{d}</span>
                  <span className="text-[9px] font-medium mt-0.5 h-3 flex items-center gap-0.5">
                    {allPaid ? (
                      <CheckCircle2 size={10} className={cn(day !== d && 'text-emerald-500')} />
                    ) : late ? (
                      t('casa.payables.chip.lateShort')
                    ) : isNext ? (
                      t('casa.payables.chip.nextShort')
                    ) : (
                      list.map((b) => <span key={b.key} className="h-1 w-1 rounded-full bg-current opacity-50" />)
                    )}
                  </span>
                </button>
              )
            })}
          </div>

          <ul className="space-y-3">
            {shown.map((b) => {
              const state = billState(b, today)
              return (
                <li key={b.key} className="flex items-start gap-2.5">
                  {state === 'paid' ? (
                    <CheckCircle2 size={18} className="text-emerald-500 shrink-0 mt-0.5" />
                  ) : state === 'late' ? (
                    <AlertTriangle size={18} className="text-rose-500 shrink-0 mt-0.5" />
                  ) : (
                    <Circle size={18} className="text-muted-foreground/50 shrink-0 mt-0.5" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className={cn('text-sm font-medium truncate', state === 'paid' && 'text-muted-foreground')}>
                        {b.description}
                      </span>
                      <span className="text-sm tabular-nums whitespace-nowrap">{money(b.amount)}</span>
                    </div>
                    <div className="text-xs text-muted-foreground flex flex-wrap gap-x-2">
                      <span className={cn(state === 'late' && 'text-rose-600 font-medium')}>
                        {state === 'paid' && b.paid_date
                          ? t('casa.bills.paidOn', { date: fmtDate(b.paid_date) })
                          : state === 'late'
                            ? t('casa.payables.lateSince', { date: fmtDate(b.due_date) })
                            : t('casa.bills.dueOn', { date: fmtDate(b.due_date) })}
                      </span>
                      <span className="inline-flex items-center gap-1">
                        <span className={cn('h-1.5 w-1.5 rounded-full', b.payer_member_id === selfId ? SELF_COLOR : OTHER_COLOR)} />
                        {b.payer_member_id === selfId ? t('casa.you') : nameOf(b.payer_member_id)}
                      </span>
                      {b.total != null && <span>{t('casa.installment', { n: b.number, total: b.total })}</span>}
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </Card>
  )
}

// ---------------------------------------------------------------------------
// The month's closing, ready to paste
// ---------------------------------------------------------------------------

export function ClosingButton({
  overview,
  money,
  fmtDate,
  monthName,
}: {
  overview: GroupOverview
  money: Money
  fmtDate: FmtDate
  monthName: string
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const text = open ? buildClosingText(overview, { t, money, fmtDate: (iso) => fmtDate(iso), monthName }) : ''

  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)} className="gap-1.5">
        <FileText size={14} /> {t('casa.closing.button')}
      </Button>
      <Dialog open={open} onOpenChange={(o) => { setOpen(o); setCopied(false) }}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>{t('casa.closing.title', { month: monthName })}</DialogTitle>
          </DialogHeader>
          <pre className="max-h-[55vh] overflow-auto whitespace-pre-wrap rounded-lg bg-muted p-3 text-xs leading-relaxed font-mono">
            {text}
          </pre>
          <div className="flex justify-end">
            <Button
              size="sm"
              className="gap-1.5"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(text)
                  setCopied(true)
                } catch {
                  setCopied(false)
                }
              }}
            >
              <ClipboardCopy size={14} /> {copied ? t('casa.closing.copied') : t('casa.closing.copy')}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}

// ---------------------------------------------------------------------------
// What if the month were split by income
// ---------------------------------------------------------------------------

const INCOME_KEY = 'casa.incomes'

function readIncomes(): [number, number] {
  try {
    const raw = JSON.parse(localStorage.getItem(INCOME_KEY) ?? 'null')
    if (Array.isArray(raw) && raw.length === 2) return [Number(raw[0]) || 0, Number(raw[1]) || 0]
  } catch {
    /* per-viewer convenience only */
  }
  return [0, 0]
}

function storeIncomes(v: [number, number]) {
  try {
    localStorage.setItem(INCOME_KEY, JSON.stringify(v))
  } catch {
    /* per-viewer convenience only */
  }
}

export function IncomeSplitSimulator({
  total,
  selfName,
  otherName,
  money,
  locale,
}: {
  total: number
  selfName: string
  otherName: string
  money: Money
  locale: string
}) {
  const { t } = useTranslation()
  const [incomes, setIncomes] = useState<[number, number]>(readIncomes)
  const [open, setOpen] = useState(() => incomes[0] + incomes[1] > 0)
  const set = (i: 0 | 1, v: number) => {
    const next: [number, number] = i === 0 ? [v, incomes[1]] : [incomes[0], v]
    setIncomes(next)
    storeIncomes(next)
  }
  const ready = incomes[0] > 0 && incomes[1] > 0
  const { pctSelf, shareSelf } = proportionalShares(total, incomes[0], incomes[1])
  const diff = shareSelf - total / 2
  const pct = (v: number) => `${v.toLocaleString(locale, { maximumFractionDigits: 1 })}%`

  return (
    <div className="mt-6 rounded-xl border border-dashed border-border">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between gap-2 px-4 py-3 text-xs font-semibold"
        aria-expanded={open}
      >
        {t('casa.income.title')}
        <ChevronDown size={14} className={cn('transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="px-4 pb-4">
          <div className="grid grid-cols-2 gap-3 mb-3">
            {([0, 1] as const).map((i) => (
              <label key={i} className="text-xs text-muted-foreground">
                {t('casa.income.of', { name: i === 0 ? selfName : otherName })}
                <input
                  type="number"
                  inputMode="decimal"
                  min={0}
                  step={100}
                  value={incomes[i] || ''}
                  onChange={(e) => set(i, Math.max(0, Number(e.target.value) || 0))}
                  className="mt-1 h-8 w-full rounded-lg border border-border bg-card px-2 text-sm text-foreground tabular-nums"
                />
              </label>
            ))}
          </div>
          {ready ? (
            <>
              <div className="flex h-5 overflow-hidden rounded-md text-[10px] font-semibold text-white">
                <div className={cn(SELF_COLOR, 'flex items-center px-2')} style={{ width: `${pctSelf}%` }}>
                  {pct(pctSelf)}
                </div>
                <div className={cn(OTHER_COLOR, 'flex flex-1 items-center justify-end px-2')}>{pct(100 - pctSelf)}</div>
              </div>
              <p className="text-xs text-muted-foreground mt-2">
                {t('casa.income.result', { amount: money(shareSelf), even: money(total / 2) })}
                {Math.abs(diff) >= 0.01 && ` (${diff > 0 ? '+' : '−'}${money(Math.abs(diff))})`}
              </p>
            </>
          ) : null}
          <p className="text-[11px] text-muted-foreground mt-2">{t('casa.income.hint')}</p>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// The last six months
// ---------------------------------------------------------------------------

export function HistoryCard({
  groupId,
  month,
  locale,
  money,
  otherName,
}: {
  groupId: string
  month: string
  locale: string
  money: Money
  otherName: string
}) {
  const { t } = useTranslation()
  const months = useMemo(() => Array.from({ length: 6 }, (_, i) => shiftMonth(month, i - 5)), [month])
  const results = useQueries({
    queries: months.map((m) => ({
      queryKey: ['groups', groupId, 'overview', m],
      queryFn: () => groupsApi.overview(groupId, m),
    })),
  })
  const data = months.map((m, i) => {
    const o = results[i].data
    const selfId = o?.self_member_id
    const self = Number(o?.paid_by.find((p) => p.member_id === selfId)?.amount ?? 0)
    const other = Number(o?.paid_by.find((p) => p.member_id !== selfId)?.amount ?? 0)
    const label = new Date(m + '-01T00:00:00').toLocaleDateString(locale, { month: 'short' }).replace('.', '')
    return { month: label, self: Math.max(self, 0), other: Math.max(other, 0), total: self + other }
  })
  const withData = data.filter((d) => d.total > 0)
  const avg = withData.length ? withData.reduce((s, d) => s + d.total, 0) / withData.length : 0
  const youLabel = t('casa.you')

  return (
    <Card>
      <CardTitle
        title={t('casa.history.title')}
        hint={t('casa.history.hint', { amount: money(avg) })}
        action={
          <div className="flex gap-3 text-xs text-muted-foreground">
            <span className="inline-flex items-center gap-1.5"><span className={cn('h-2 w-2 rounded-full', SELF_COLOR)} />{youLabel}</span>
            <span className="inline-flex items-center gap-1.5"><span className={cn('h-2 w-2 rounded-full', OTHER_COLOR)} />{otherName}</span>
          </div>
        }
      />
      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
            <CartesianGrid vertical={false} stroke="currentColor" strokeOpacity={0.08} />
            <XAxis dataKey="month" tickLine={false} axisLine={false} fontSize={11} stroke="currentColor" />
            <YAxis
              tickLine={false}
              axisLine={false}
              fontSize={11}
              width={44}
              stroke="currentColor"
              tickFormatter={(v: number) => (v >= 1000 ? `${(v / 1000).toLocaleString(locale, { maximumFractionDigits: 1 })}k` : String(v))}
            />
            <Tooltip
              cursor={{ fill: 'currentColor', fillOpacity: 0.05 }}
              formatter={(v, name) => [money(Number(v)), name === 'self' ? youLabel : otherName]}
              contentStyle={{ borderRadius: 12, fontSize: 12 }}
            />
            <Bar dataKey="self" stackId="a" fill={SELF_HEX} />
            <Bar dataKey="other" stackId="a" fill={OTHER_HEX} radius={[6, 6, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Compact card on the dashboard
// ---------------------------------------------------------------------------

function readStoredGroup(): string | null {
  try {
    return localStorage.getItem('casa.groupId')
  } catch {
    return null
  }
}

export function CasaMiniCard({ locale }: { locale: string }) {
  const { t } = useTranslation()
  const month = currentMonth()
  const { data: groupList } = useQuery({
    queryKey: ['groups', 'active-list'],
    queryFn: () => groupsApi.list(false),
  })
  const stored = readStoredGroup()
  const groupId = groupList?.some((g) => g.id === stored) ? stored : (groupList?.[0]?.id ?? null)
  const { data: overview } = useQuery({
    queryKey: ['groups', groupId, 'overview', month],
    queryFn: () => groupsApi.overview(groupId!, month),
    enabled: !!groupId,
  })
  if (!groupId || !overview) return null

  const money = (v: number) => formatCurrency(v, overview.default_currency, locale)
  const names = new Map(overview.members.map((m) => [m.id, m.name]))
  const line = overview.balance_lines[0]
  const owed = line ? Number(line.amount) : 0
  const today = todayIso()
  const open = buildPayBills(overview).filter((b) => !b.paid).slice(0, 3)
  const group = groupList?.find((g) => g.id === groupId)

  return (
    <Link
      to="/casa"
      className="block bg-card rounded-xl border border-border shadow-sm p-4 mb-5 hover:border-primary/40 transition-colors"
    >
      <div className="flex flex-col sm:flex-row sm:items-center gap-4">
        <div className="flex items-center gap-3 sm:w-72 shrink-0">
          <span className="h-10 w-10 rounded-xl bg-pink-50 dark:bg-pink-950/40 text-pink-600 flex items-center justify-center shrink-0">
            <HeartHandshake size={20} />
          </span>
          <div className="min-w-0">
            <p className="text-xs text-muted-foreground truncate">{group?.name ?? t('casa.title')}</p>
            {owed === 0 ? (
              <p className="text-sm font-semibold text-emerald-600">{t('casa.settle.allSettled')}</p>
            ) : (
              <p className={cn('text-sm font-semibold', owed < 0 ? 'text-rose-500' : 'text-emerald-600')}>
                {owed < 0
                  ? t('casa.mini.youOwe', { name: names.get(line.member_id), amount: money(-owed) })
                  : t('casa.mini.owesYou', { name: names.get(line.member_id), amount: money(owed) })}
              </p>
            )}
          </div>
        </div>
        {open.length === 0 ? (
          <p className="flex-1 text-sm text-muted-foreground">{t('casa.payables.allPaid')}</p>
        ) : (
          <ul className="flex-1 grid gap-1.5 sm:grid-cols-3">
            {open.map((b) => {
              const d = daysBetween(today, b.due_date)
              return (
                <li key={b.key} className="rounded-lg bg-muted/60 px-2.5 py-1.5 min-w-0">
                  <p className="text-xs font-medium truncate">{b.description}</p>
                  <p className={cn('text-[11px] tabular-nums', d < 0 ? 'text-rose-600' : 'text-muted-foreground')}>
                    {money(b.amount)} ·{' '}
                    {d < 0 ? t('casa.payables.chip.lateShort') : d === 0 ? t('casa.payables.dueToday') : t('casa.payables.dueIn', { count: d })}
                  </p>
                </li>
              )
            })}
          </ul>
        )}
        <ArrowRight size={16} className="hidden sm:block text-muted-foreground shrink-0" />
      </div>
    </Link>
  )
}
