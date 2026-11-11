import { describe, expect, it } from 'vitest'

import {
  billsByDay,
  buildAlerts,
  buildClosingText,
  buildPayBills,
  daysBetween,
  nextBill,
  proportionalShares,
  todayIso,
} from './casa'
import type { GroupOverview } from '@/types'

const ME = 'me'
const HER = 'her'

function overview(over: Partial<GroupOverview> = {}): GroupOverview {
  return {
    group_id: 'g',
    month: '2026-11',
    default_currency: 'BRL',
    self_member_id: ME,
    members: [{ id: ME, name: 'Matheus' }, { id: HER, name: 'Yanna' }],
    balance_lines: [],
    upcoming_lines: [],
    total: '0',
    paid_by: [],
    share_of: [],
    items: [],
    bills: [],
    contracts: [],
    ...over,
  }
}

const bill = (id: string, due: string, paid = false) => ({
  recurring_id: id,
  description: id,
  due_date: due,
  amount: '100.00',
  payer_member_id: ME,
  paid,
  paid_date: paid ? due : null,
  number: 1,
  total: null,
})

// Echoes the key and its values, so the text's structure is what is checked.
const t = (key: string, opts: Record<string, unknown> = {}) =>
  `${key.replace('casa.closing.', '')}(${Object.values(opts).join('|')})`

describe('casa helpers', () => {
  it('formats today in local time and counts days', () => {
    expect(todayIso(new Date(2026, 10, 5))).toBe('2026-11-05')
    expect(daysBetween('2026-11-11', '2026-11-30')).toBe(19)
    expect(daysBetween('2026-11-11', '2026-11-10')).toBe(-1)
  })

  it('sorts bills by due date and groups them by day', () => {
    const bills = buildPayBills(overview({
      bills: [bill('B', '2026-11-30'), bill('A', '2026-11-10', true), bill('C', '2026-11-30')],
    }))
    expect(bills.map((b) => b.description)).toEqual(['A', 'B', 'C'])
    expect(billsByDay(bills).map(([d, l]) => [d, l.length])).toEqual([[10, 1], [30, 2]])
  })

  it('picks the next unpaid bill, falling back to a late one', () => {
    const bills = buildPayBills(overview({
      bills: [bill('late', '2026-11-05'), bill('paid', '2026-11-10', true), bill('soon', '2026-11-30')],
    }))
    expect(nextBill(bills, '2026-11-11')?.description).toBe('soon')
    expect(nextBill(bills.slice(0, 2), '2026-11-11')?.description).toBe('late')
  })

  it('alerts only for late bills, bills due soon and transfers to record', () => {
    const bills = buildPayBills(overview({
      bills: [
        bill('late', '2026-11-05'),
        bill('paid', '2026-11-06', true),
        bill('soon', '2026-11-15'),
        bill('far', '2026-11-30'),
      ],
    }))
    expect(buildAlerts(bills, '2026-11-11', 0)).toEqual([
      { kind: 'late', bill: bills[0], days: 6 },
      { kind: 'soon', bill: bills[2], days: 4 },
    ])
    expect(buildAlerts([], '2026-11-11', 0)).toEqual([])
    expect(buildAlerts([], '2026-11-11', 2)).toEqual([{ kind: 'suggestions', count: 2 }])
  })

  it('writes the closing: hers first, totals, shares and who owes', () => {
    const o = overview({
      total: '300',
      paid_by: [{ member_id: ME, amount: '100' }, { member_id: HER, amount: '200' }],
      share_of: [{ member_id: ME, amount: '150' }, { member_id: HER, amount: '150' }],
      balance_lines: [{ member_id: HER, currency: 'BRL', amount: '-50' }],
      items: [
        { transaction_id: '1', date: '2026-11-10', description: 'Chácara', category_name: null, category_color: null, category_icon: null, type: 'debit', amount: '200', payer_member_id: HER, status: 'paid', recurring: true, my_share: '100' },
        { transaction_id: '2', date: '2026-11-08', description: 'Internet', category_name: null, category_color: null, category_icon: null, type: 'debit', amount: '100', payer_member_id: ME, status: 'paid', recurring: false, my_share: '50' },
      ],
    } as unknown as Partial<GroupOverview>)
    const text = buildClosingText(o, {
      t, money: (v) => `R$${v}`, fmtDate: (iso) => iso.slice(8), monthName: 'novembro',
    })
    expect(text.split('\n')).toEqual([
      '*heading(novembro)*',
      '',
      'paidBy(Yanna)',
      '• Chácara (10): R$200',
      'totalOf(Yanna|R$200)',
      '',
      'paidBy(Matheus)',
      '• Internet (08): R$100',
      'totalOf(Matheus|R$100)',
      '',
      'houseTotal(R$300)',
      'shareOf(Matheus|R$150)',
      'shareOf(Yanna|R$150)',
      '',
      'pending(Matheus|Yanna|R$50)',
    ])
  })

  it('splits by income, evenly when no income is known', () => {
    expect(proportionalShares(1000, 6000, 4000)).toEqual({ pctSelf: 60, shareSelf: 600, shareOther: 400 })
    expect(proportionalShares(1000, 0, 0).shareSelf).toBe(500)
  })
})
