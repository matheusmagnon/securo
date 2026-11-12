import uuid
from datetime import date as _Date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field

GroupKind = Literal["social", "cost_center", "project", "client", "other"]


class GroupBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: GroupKind = "social"
    default_currency: str = Field(default="USD", min_length=3, max_length=3)
    icon: str = "users"
    color: str = "#6B7280"
    notes: Optional[str] = None


class GroupCreate(GroupBase):
    pass


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    kind: Optional[GroupKind] = None
    default_currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    icon: Optional[str] = None
    color: Optional[str] = None
    notes: Optional[str] = None
    is_archived: Optional[bool] = None


class GroupMemberBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    linked_user_id: Optional[uuid.UUID] = None
    email: Optional[EmailStr] = None
    is_self: bool = False


class GroupMemberCreate(GroupMemberBase):
    pass


class GroupMemberUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    linked_user_id: Optional[uuid.UUID] = None
    email: Optional[EmailStr] = None
    is_self: Optional[bool] = None


class GroupMemberRead(GroupMemberBase):
    id: uuid.UUID
    group_id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GroupRead(GroupBase):
    id: uuid.UUID
    user_id: uuid.UUID
    is_archived: bool
    # Derived per-request: True if the requester is the group owner.
    # Linked members get False — the frontend uses this to hide edit UI.
    is_owner: bool = True
    created_at: datetime
    members: list[GroupMemberRead] = []

    model_config = ConfigDict(from_attributes=True)


class GroupBalanceLine(BaseModel):
    member_id: uuid.UUID
    currency: str
    # Positive = member owes the group owner. Negative = owner owes member.
    amount: Decimal
    # Same value FX-converted to the group's default currency, so the
    # frontend can roll up cross-currency lines into a single KPI.
    amount_in_default_currency: Decimal


class GroupBalances(BaseModel):
    group_id: uuid.UUID
    self_member_id: Optional[uuid.UUID] = None
    default_currency: str
    # Owed now: shares of transactions dated up to today, minus settlements.
    lines: list[GroupBalanceLine] = []
    # Shares of later-dated transactions (installments reported ahead of
    # time), owed once their date arrives.
    upcoming_lines: list[GroupBalanceLine] = []


class NextSettlementShare(BaseModel):
    member_id: uuid.UUID
    name: Optional[str] = None
    amount: Decimal


class NextSettlementNet(BaseModel):
    member_id: uuid.UUID
    amount: Decimal


class NextSettlementItem(BaseModel):
    transaction_id: uuid.UUID
    date: _Date
    description: str
    amount: Decimal
    currency: str
    type: str
    installment: Optional[str] = None
    payer_member_id: Optional[uuid.UUID] = None
    payer_name: Optional[str] = None
    shares: list[NextSettlementShare] = []
    # card_bill (due date of its card bill), card (bill unknown), paid,
    # upcoming (dated later), planned (recurring placeholder, not paid).
    reference_kind: str
    reference_date: _Date
    bill_state: Optional[str] = None
    estimated: bool = False
    confirmed: bool
    bucket: Literal["next", "later"]
    # Caller's view: + the others owe you, - you owe them.
    you_net: Decimal
    net_by_member: list[NextSettlementNet] = []


class NextSettlementSettlement(BaseModel):
    id: uuid.UUID
    date: _Date
    amount: Decimal
    from_member_id: uuid.UUID
    to_member_id: uuid.UUID
    item_count: int


class NextSettlement(BaseModel):
    group_id: uuid.UUID
    self_member_id: Optional[uuid.UUID] = None
    currency: str
    today: _Date
    last_settlement_date: Optional[_Date] = None
    next_settlement_date: Optional[_Date] = None
    items: list[NextSettlementItem] = []
    next_confirmed_total: Decimal
    next_total_with_planned: Decimal
    later_total: Decimal
    settlements: list[NextSettlementSettlement] = []


class GroupSummaryMonth(BaseModel):
    # "YYYY-MM"; the client formats it for display.
    month: str
    total: Decimal


class GroupSummaryCategory(BaseModel):
    # Null for transactions with no category.
    category_id: Optional[uuid.UUID] = None
    name: Optional[str] = None
    color: Optional[str] = None
    total: Decimal


class GroupSummary(BaseModel):
    group_id: uuid.UUID
    default_currency: str
    total_moved: Decimal
    months: list[GroupSummaryMonth] = []
    categories: list[GroupSummaryCategory] = []


class GroupOverviewMember(BaseModel):
    id: uuid.UUID
    name: str


class GroupOverviewAmount(BaseModel):
    member_id: uuid.UUID
    amount: Decimal


class GroupOverviewItem(BaseModel):
    transaction_id: uuid.UUID
    date: _Date
    description: str
    category_name: Optional[str] = None
    category_color: Optional[str] = None
    category_icon: Optional[str] = None
    type: str
    amount: Decimal
    payer_member_id: Optional[uuid.UUID] = None
    # planned | pending | upcoming | paid
    status: str
    recurring: bool = False
    my_share: Optional[Decimal] = None


class GroupOverviewBill(BaseModel):
    recurring_id: uuid.UUID
    description: str
    due_date: _Date
    amount: Decimal
    payer_member_id: Optional[uuid.UUID] = None
    paid: bool
    paid_date: Optional[_Date] = None
    number: int
    total: Optional[int] = None


class GroupOverviewContract(BaseModel):
    recurring_id: uuid.UUID
    description: str
    amount: Decimal
    frequency: str
    payer_member_id: Optional[uuid.UUID] = None
    done: int
    total: int
    remaining_amount: Decimal
    end_date: _Date
    next_date: _Date


class GroupOverview(BaseModel):
    """One month of the group: cost, who paid, bills and contracts."""

    group_id: uuid.UUID
    month: str
    default_currency: str
    self_member_id: Optional[uuid.UUID] = None
    members: list[GroupOverviewMember]
    balance_lines: list[GroupBalanceLine] = []
    upcoming_lines: list[GroupBalanceLine] = []
    total: Decimal
    paid_by: list[GroupOverviewAmount]
    share_of: list[GroupOverviewAmount]
    items: list[GroupOverviewItem]
    bills: list[GroupOverviewBill]
    contracts: list[GroupOverviewContract]
