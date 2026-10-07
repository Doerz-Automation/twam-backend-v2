import pytest
import os
from decimal import Decimal
from datetime import timedelta, date, datetime, timezone as dt_timezone
from django.utils import timezone
from freezegun import freeze_time

from api.models import (
    User, AdvertisementChannel, Advertisement, AdvertisementChannelAssignment,
    AdvertisementFlag, ChannelDiscountHistory, MonthlyInvoice, InvoiceLineItem
)
from api.tasks import run_monthly_billing, apply_pending_discount_changes

# --- Fixtures ---

@pytest.fixture
def user(db):
    u = User.objects.create_user(email="ad_tester@example.com", password="password")
    u.is_verified = True
    u.stripe_customer_id = "cus_mock_pytest"
    u.save()
    return u

@pytest.fixture
def base_channel(db):
    # Channel costs exactly $100.00/day
    return AdvertisementChannel.objects.create(
        channel_name="Premium Billboard",
        price_per_day=Decimal("100.00"),
        start_date=date(2026, 1, 1),
        status="active",
        apply_discount=False,
        discount_price=Decimal("0.00")
    )

@pytest.fixture
def add_ad_to_channel(db, user, base_channel):
    def _create(start_dt, end_dt=None):
        ad = Advertisement.objects.create(
            user=user,
            advertisment_name="My Startup Ad",
            start_date=start_dt,
            end_date=end_dt,
            advertisment_cost=Decimal("0.00"),
            advertisement_status="active"
        )
        AdvertisementChannelAssignment.objects.create(
            advertisement=ad,
            advertisement_channel=base_channel
        )
        return ad
    return _create

# --- Tests ---

@pytest.mark.django_db
class TestAdvancedBillingLogics:

    # 1. The Midnight Rollover (State Change)
    def test_midnight_rollover_state_change(self, user, base_channel, add_ad_to_channel):
        """
        Create an ad. Mock the time to 11:50 PM. Apply a 3% channel discount.
        Verify current day cost has no discount.
        Advance to 12:01 AM next day. Verify new cost is exactly 3% less.
        """
        ad_start = date(2026, 6, 10)
        base_channel.start_date = ad_start
        base_channel.save()
        ad = add_ad_to_channel(start_dt=ad_start)

        # 11:50 PM UTC on June 15th
        with freeze_time("2026-06-15 23:50:00"):
            # Admin applies a 3% discount (3% off $100 = $97.00) starting tomorrow
            base_channel.pending_apply_discount = True
            base_channel.pending_discount_price = Decimal("3.00")
            base_channel.discount_effective_from = date(2026, 6, 16)
            base_channel.save()

            # For June 15th, it should still be full price ($100.00)
            cost_june_15 = ad.get_daily_rate(date(2026, 6, 15))
            assert cost_june_15 == Decimal("100.00")

        # 12:01 AM UTC on June 16th (Midnight rolled over)
        with freeze_time("2026-06-16 00:01:00"):
            apply_pending_discount_changes()
            
            base_channel.refresh_from_db()
            cost_june_16 = ad.get_daily_rate(date(2026, 6, 16))
            assert cost_june_16 == Decimal("97.00")

            # Historical should remain correct
            assert ad.get_daily_rate(date(2026, 6, 15)) == Decimal("100.00")

    # 2. The Cross-Month Invoice Split
    def test_cross_month_invoice_split(self, user, base_channel, add_ad_to_channel):
        """
        Ad starts Jan 25th, runs until Feb 5th.
        Trigger billing on Feb 1st midnight.
        Verify invoice charges for Jan 25-31 (7 days).
        """
        base_channel.start_date = date(2026, 1, 1)
        base_channel.save()
        ad = add_ad_to_channel(start_dt=date(2026, 1, 25), end_dt=date(2026, 2, 5))

        with freeze_time("2026-02-01 00:00:00"):
            run_monthly_billing()

        invoice = MonthlyInvoice.objects.get(user=user)
        # 7 days * $100 = $700
        assert invoice.total_amount == Decimal("700.00")
        assert invoice.start_date == date(2026, 1, 1)
        assert invoice.end_date == date(2026, 1, 31)

    # 3. Fractional Flagging Exclusions
    def test_fractional_flagging_exclusions(self, user, base_channel, add_ad_to_channel):
        """
        10-day ad. Flag Day 3, Unflag Day 5. Billable = 8 days.
        """
        ad = add_ad_to_channel(start_dt=date(2026, 10, 1), end_dt=date(2026, 10, 10))

        # Flag on Day 3 (Oct 3)
        flag_dt = datetime(2026, 10, 3, 10, 0, tzinfo=dt_timezone.utc)
        # Unflag on Day 5 (Oct 5)
        resolve_dt = datetime(2026, 10, 5, 10, 0, tzinfo=dt_timezone.utc)

        AdvertisementFlag.objects.create(
            advertisement=ad,
            flagged_at=flag_dt,
            resolved=True,
            resolved_at=resolve_dt
        )

        with freeze_time("2026-11-01 00:00:00"):
            run_monthly_billing()

        invoice = MonthlyInvoice.objects.get(user=user)
        # Excludes Oct 3 and 4. (Oct 5 is when it was resolved, so it's active again)
        # Oct 1, 2, 5, 6, 7, 8, 9, 10 = 8 days.
        assert invoice.total_amount == Decimal("800.00")

    # 4. The Channel Death Cascade
    def test_channel_death_cascade(self, user, base_channel, add_ad_to_channel):
        """
        Channel expires Nov 15th. Ad expires Nov 20th.
        Advance to Nov 16th. Cost = 0 for 16-20.
        """
        base_channel.end_date = date(2026, 11, 15)
        base_channel.save()
        ad = add_ad_to_channel(start_dt=date(2026, 11, 1), end_dt=date(2026, 11, 20))

        from api.scheduler import update_channel_status
        with freeze_time("2026-11-16 01:00:00"):
            update_channel_status()
            base_channel.refresh_from_db()
            ad.refresh_from_db()

            assert base_channel.status == "phaseout"
            # Since channel is phased out, rate should be 0 on 16th
            assert ad.get_daily_rate(date(2026, 11, 16)) == Decimal("0.00")
            # But 15th should still be $100
            assert ad.get_daily_rate(date(2026, 11, 15)) == Decimal("100.00")

    # 5. Dynamic Accrual (Current Month View)
    def test_dynamic_accrual_current_month_view(self, user, base_channel, add_ad_to_channel):
        """
        Today is 15th. Ad since 1st. Discount 5-10th ($50).
        Compare dynamic sum.
        """
        ad = add_ad_to_channel(start_dt=date(2026, 8, 1))

        # 1st-4th: $100 (4 days)
        # 5th-10th: $50 (6 days)
        # 11th-15th: $100 (5 days)
        # Total = 400 + 300 + 500 = 1200
        
        ChannelDiscountHistory.objects.create(
            channel=base_channel,
            apply_discount=False,
            discount_price=Decimal("0.00"),
            effective_from=date(2026, 8, 1),
            effective_until=date(2026, 8, 4)
        )
        ChannelDiscountHistory.objects.create(
            channel=base_channel,
            apply_discount=True,
            discount_price=Decimal("50.00"),
            effective_from=date(2026, 8, 5),
            effective_until=date(2026, 8, 10)
        )
        ChannelDiscountHistory.objects.create(
            channel=base_channel,
            apply_discount=False,
            discount_price=Decimal("0.00"),
            effective_from=date(2026, 8, 11),
            effective_until=None
        )

        with freeze_time("2026-08-15 12:00:00"):
            total_accrued = Decimal("0.00")
            curr = date(2026, 8, 1)
            end = date(2026, 8, 15)
            while curr <= end:
                total_accrued += ad.get_daily_rate(curr)
                curr += timedelta(days=1)
            
            assert total_accrued == Decimal("1200.00")
