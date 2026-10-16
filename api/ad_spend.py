"""
Per-advertisement spend for one month.

Every figure comes from AdvertisementChannel.rate_for_date, the same function
Advertisement.get_daily_rate (and therefore the monthly invoices) uses, so what
a business sees here always agrees with what it is billed.
"""
import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import List, Optional

from django.db.models import Min

from api.models import Advertisement
from api.utils import calculate_flagged_days, last_finalized_billing_day

# 'paid' is an ad that has ended and whose last invoice was paid; it still
# belongs in the months it ran. Canceled ads never ran, so they are left out.
BILLED_STATUSES = ["pending", "active", "phaseout", "paid", "past_due"]

CENT = Decimal("0.01")


@dataclass
class Segment:
    """A run of consecutive days on one channel with the same price and discount."""
    channel_name: str
    start_date: date
    end_date: date
    days: int
    price_per_day: Decimal
    discount: Decimal
    flagged: bool
    raw_cost: Decimal = Decimal("0.00")
    raw_savings: Decimal = Decimal("0.00")


@dataclass
class AdSpend:
    advertisement: Advertisement
    period_start: date
    period_end: date
    days: int
    flagged_days: int
    segments: List[Segment] = field(default_factory=list)

    @property
    def subtotal(self) -> Decimal:
        return sum((s.raw_cost for s in self.segments), Decimal("0.00")).quantize(CENT)

    @property
    def savings(self) -> Decimal:
        return sum((s.raw_savings for s in self.segments), Decimal("0.00")).quantize(CENT)


def month_bounds(year: int, month: int):
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def build_ad_spend(user, year: int, month: int, through: Optional[date] = None) -> List[AdSpend]:
    """The business's ads for one month, each with its per-channel cost segments."""
    month_start, month_end = month_bounds(year, month)
    through = through or last_finalized_billing_day()
    last_day = min(month_end, through)
    if last_day < month_start:
        return []

    ads = Advertisement.objects.filter(
        user=user,
        billing_status__in=BILLED_STATUSES,
        start_date__lte=last_day,
    ).exclude(
        end_date__lt=month_start
    ).prefetch_related(
        "channel_assignments__advertisement_channel__discount_history",
        "channel_assignments__advertisement_channel__price_history",
        "flags",
    ).order_by("start_date", "id")

    result = []
    for ad in ads:
        ad_start = max(ad.start_date, month_start)
        ad_end = min(ad.end_date, last_day) if ad.end_date else last_day
        if ad_end < ad_start:
            continue

        flagged = calculate_flagged_days(ad, ad_start, ad_end)
        entry = AdSpend(
            advertisement=ad,
            period_start=ad_start,
            period_end=ad_end,
            days=(ad_end - ad_start).days + 1,
            flagged_days=len(flagged),
        )

        for assignment in ad.channel_assignments.all():
            channel = assignment.advertisement_channel
            current: Optional[Segment] = None
            day = ad_start
            while day <= ad_end:
                if channel.end_date and day > channel.end_date:
                    break
                base, discount, net = channel.rate_for_date(day)
                is_flagged = day in flagged
                same_run = (
                    current is not None
                    and current.end_date == day - timedelta(days=1)
                    and current.price_per_day == base
                    and current.discount == discount
                    and current.flagged == is_flagged
                )
                if not same_run:
                    current = Segment(
                        channel_name=channel.channel_name, start_date=day, end_date=day, days=0,
                        price_per_day=base, discount=discount, flagged=is_flagged,
                    )
                    entry.segments.append(current)
                current.end_date = day
                current.days += 1
                if not is_flagged:
                    current.raw_cost += net
                    current.raw_savings += base - net
                day += timedelta(days=1)

        if entry.segments:
            result.append(entry)
    return result


def month_total(entries: List[AdSpend]) -> Decimal:
    """Sum of the ad subtotals, so the total always equals what the rows add up to."""
    return sum((e.subtotal for e in entries), Decimal("0.00"))


def earliest_spend_month(user) -> Optional[str]:
    """'YYYY-MM' of the first month the business had an ad, or None if it never had one.
    The page uses it so the month picker cannot go back past where the data starts."""
    first = Advertisement.objects.filter(
        user=user, billing_status__in=BILLED_STATUSES
    ).aggregate(first=Min("start_date"))["first"]
    return f"{first.year}-{first.month:02d}" if first else None
