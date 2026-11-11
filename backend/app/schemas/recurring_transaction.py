import uuid
from datetime import date as _Date
from decimal import Decimal
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.transaction_category_line import (
    TransactionCategoryLineRead,
    TransactionCategoryLinesInput,
)

WeekendAdjustment = Literal["none", "previous_friday", "next_monday"]
RecurringFrequency = Literal["weekly", "biweekly", "monthly", "quarterly", "semiannual", "yearly"]
DayOfMonth = Annotated[int, Field(ge=1, le=31)]


class RecurringTransactionCreate(BaseModel):
    description: str
    amount: Decimal
    currency: str = "USD"
    type: str  # debit, credit
    frequency: str  # weekly, biweekly, monthly, quarterly, semiannual, yearly
    weekend_adjustment: WeekendAdjustment = "none"
    day_of_month: Optional[DayOfMonth] = None
    start_date: _Date
    end_date: Optional[_Date] = None
    account_id: uuid.UUID
    category_id: Optional[uuid.UUID] = None
    skip_first: bool = False  # Set true when first occurrence already created as a transaction
    auto_generate: bool = True  # Materialize occurrences; when false, wait for the real charge
    # Generate each occurrence on the 1st of its month (shows as upcoming).
    generate_at_month_start: bool = False
    # Paid any day of its month: a charge of the same month answers it.
    match_whole_month: bool = False
    # Contract installments paid before this bill existed.
    installment_offset: int = Field(default=0, ge=0, le=1000)
    # Split every occurrence equally among this group's members.
    split_group_id: Optional[uuid.UUID] = None
    # Split across categories; every occurrence starts with these parts.
    category_lines: Optional[TransactionCategoryLinesInput] = None


class RecurringTransactionUpdate(BaseModel):
    description: Optional[str] = None
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    type: Optional[str] = None
    frequency: Optional[RecurringFrequency] = None
    weekend_adjustment: Optional[WeekendAdjustment] = None
    day_of_month: Optional[DayOfMonth] = None
    start_date: Optional[_Date] = None
    end_date: Optional[_Date] = None
    account_id: Optional[uuid.UUID] = None
    category_id: Optional[uuid.UUID] = None
    is_active: Optional[bool] = None
    auto_generate: Optional[bool] = None
    generate_at_month_start: Optional[bool] = None
    match_whole_month: Optional[bool] = None
    installment_offset: Optional[int] = Field(default=None, ge=0, le=1000)
    # null stops splitting later occurrences.
    split_group_id: Optional[uuid.UUID] = None
    category_lines: Optional[TransactionCategoryLinesInput] = None


class RecurringTransactionRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    account_id: Optional[uuid.UUID] = None
    category_id: Optional[uuid.UUID] = None
    description: str
    amount: Decimal
    currency: str
    type: str
    frequency: str
    weekend_adjustment: WeekendAdjustment = "none"
    day_of_month: Optional[int] = None
    start_date: _Date
    end_date: Optional[_Date] = None
    is_active: bool
    auto_generate: bool = True
    generate_at_month_start: bool = False
    match_whole_month: bool = False
    installment_offset: int = 0
    split_group_id: Optional[uuid.UUID] = None
    next_occurrence: _Date
    # Installment progress: occurrences already behind the bill (generated or
    # paid) and, for a bill with an end date, how many there are in all.
    occurrences_done: int = 0
    occurrences_total: Optional[int] = None
    amount_primary: Optional[float] = None
    fx_rate_used: Optional[float] = None
    has_category_lines: bool = False
    category_lines: list[TransactionCategoryLineRead] = []

    model_config = ConfigDict(from_attributes=True)
