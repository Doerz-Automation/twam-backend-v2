from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from api.models import Advertisement, MonthlyInvoice, User, InvoiceLineItem
from api.stripe_services import charge_customer_off_session, get_user_payment_methods
import logging

logger = logging.getLogger(__name__)


def get_previous_month_range():
    """
    Returns (start_date, end_date) for the previous month.
    Example: If today is Feb 1st, returns (Jan 1st, Jan 31st).
    """
    today = timezone.now().date()
    # first day of this month
    this_month_start = today.replace(day=1)
    # last day of previous month = this_month_start - 1 day
    last_month_end = this_month_start - timedelta(days=1)
    # first day of previous month
    last_month_start = last_month_end.replace(day=1)
    return last_month_start, last_month_end

def calculate_flagged_days(ad, start_date, end_date):
    """
    Returns a SET of calendar dates on which the ad was flagged during
    [start_date, end_date]. Used by the billing loop for O(1) per-day lookup.

    ad.flags.all() use karta hai taake prefetch cache hit ho.
    .filter() call karne se Django cache bypass karke fresh DB query karta hai,
    isliye filtering Python mein ki gayi hai.
    """
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=dt_timezone.utc)
    end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=dt_timezone.utc)

    # .all() prefetch cache use karta hai — .filter() nahi karta
    flags = [
        f for f in ad.flags.all()
        if f.flagged_at <= end_dt
        and (f.resolved_at is None or f.resolved_at >= start_dt)
    ]

    flagged_dates = set()

    for flag in flags:
        flag_start = flag.flagged_at.date()
        if flag_start < start_date:
            flag_start = start_date

        flag_end = end_date
        if flag.resolved and flag.resolved_at:
            # resolved_at is when the flag was cleared — ad is active again from that day.
            # So the last FLAGGED day is resolved_at.date() - 1.
            flag_end_date = flag.resolved_at.date() - timedelta(days=1)
            if flag_end_date < end_date:
                flag_end = flag_end_date

        current = flag_start
        while current <= flag_end:
            flagged_dates.add(current)
            current += timedelta(days=1)

    return flagged_dates

def run_monthly_billing():
    bill_start_date, bill_end_date = get_previous_month_range()
    logger.info(f"Starting Monthly Billing for period: {bill_start_date} to {bill_end_date}")
    
    # ... (query remains same) ...
    pending_ads = Advertisement.objects.filter(
        billing_status__in=['pending', 'active'],
        start_date__lte=bill_end_date
    ).exclude(
        end_date__lt=bill_start_date
    ).select_related('user').prefetch_related(
        'channel_assignments__advertisement_channel__discount_history',
        'channel_assignments__advertisement_channel__price_history',
    )
    
    user_ads_map = {}
    for ad in pending_ads:
        if ad.user_id not in user_ads_map:
            user_ads_map[ad.user_id] = []
        user_ads_map[ad.user_id].append(ad)
        
    for user_id, ads in user_ads_map.items():
        try:
            user = User.objects.get(id=user_id)
            
            # Idempotency Check: Skip if ANY invoice exists for this period that is NOT 'failed' (e.g., 'paid' or 'pending')
            existing_invoice = MonthlyInvoice.objects.filter(
                user=user, 
                start_date=bill_start_date, 
                end_date=bill_end_date
            ).exclude(status='failed').exists()
            
            if existing_invoice:
                logger.info(f"Invoice already exists for {user.email} for {bill_start_date.strftime('%b %Y')}. Skipping creation.")
                continue

            total_amount = Decimal('0.00')
            billable_items = [] # (ad, cost, start, end)

            for ad in ads:
                active_start = max(ad.start_date, bill_start_date)
                current_ad_end = ad.end_date if ad.end_date else bill_end_date
                active_end = min(current_ad_end, bill_end_date)

                if active_end < active_start:
                    continue

                # Build a set of flagged dates for O(1) per-day lookup
                flagged_date_set = calculate_flagged_days(ad, active_start, active_end)
                days_active = (active_end - active_start).days + 1
                days_billable = days_active - len(flagged_date_set)

                if days_billable <= 0:
                    continue

                # Day-by-day loop: picks up the correct discount for each date
                # via get_daily_rate(for_date), powered by ChannelDiscountHistory.
                ad_cost = Decimal('0.00')
                flagged_days_count = 0
                current_day = active_start
                while current_day <= active_end:
                    if current_day not in flagged_date_set:
                        ad_cost += ad.get_daily_rate(for_date=current_day)
                    else:
                        flagged_days_count += 1
                    current_day += timedelta(days=1)

                if ad_cost > 0:
                    total_amount += ad_cost
                    description = f"Ad: {ad.id} ({active_start} to {active_end})"
                    if flagged_days_count > 0:
                        description += f" - {flagged_days_count} days flagged (excluded)"

                    billable_items.append({
                        'ad': ad,
                        'cost': ad_cost,
                        'start': active_start,
                        'end': active_end,
                        'description': description
                    })

            if not billable_items:
                continue

            logger.info(f"User {user.email}: Billing ${total_amount} ({len(billable_items)} ads)")

            with transaction.atomic():
                invoice = MonthlyInvoice.objects.create(
                    user=user,
                    start_date=bill_start_date,
                    end_date=bill_end_date,
                    total_amount=total_amount,
                    status='pending'
                )
                
                # Create Line Items
                for item in billable_items:
                    ad = item['ad']
                    InvoiceLineItem.objects.create(
                        invoice=invoice,
                        advertisement=ad,
                        item_type='advertisement',
                        description=item['description'],
                        amount=item['cost'],
                        period_start=item['start'],
                        period_end=item['end']
                    )
                    # We DO NOT set ad.invoice anymore!

                # Notify: invoice generated
                from api.notification_service import create_notification
                create_notification(
                    recipient=user,
                    notification_type="invoice_generated",
                    title="New invoice generated",
                    message=f"Your invoice for ${total_amount:.2f} ({bill_start_date.strftime('%b %Y')}) has been generated.",
                    invoice_id=invoice.id,
                )
            
            # Charge Stripe
            amount_cents = int(round(total_amount * 100))
            methods = get_user_payment_methods(user)
            
            if not methods:
                invoice.status = 'failed'
                invoice.save()
                logger.error(f"User {user.email}: No payment method.")
                continue

            try:
                pi_id = charge_customer_off_session(
                    customer_id=user.stripe_customer_id,
                    amount_cents=amount_cents,
                    payment_method_id=methods[0].id,
                    description=f"TWAM Invoice {bill_start_date.strftime('%b %Y')}",
                    metadata={"invoice_id": invoice.id}
                )
                
                with transaction.atomic():
                    invoice.status = 'paid'
                    invoice.stripe_payment_intent_id = pi_id
                    invoice.save()
                    
                    for item in billable_items:
                        ad = item['ad']
                        
                        # Update billing status
                        # If ad ends within this billing period (or earlier), mark as paid/complete
                        # Otherwise keep/set as active for next month
                        if ad.end_date and ad.end_date <= bill_end_date:
                            ad.billing_status = 'paid'
                        else:
                            ad.billing_status = 'active'
                            
                        ad.last_billed_at = timezone.now()
                        ad.save()

                # Notify: payment success on monthly billing
                from api.notification_service import create_notification
                create_notification(
                    recipient=user,
                    notification_type="payment_success",
                    title="Monthly payment successful",
                    message=f"Your monthly bill of ${total_amount:.2f} for {bill_start_date.strftime('%b %Y')} has been charged successfully.",
                    invoice_id=invoice.id,
                )
                        
            except Exception as e:
                logger.error(f"Payment Failed for {user.email}: {e}")
                invoice.start_grace_period(days=3)
                logger.warning(f"Invoice {invoice.id} moved to grace period.")

                # Notify: grace period started from billing
                from api.notification_service import create_notification
                create_notification(
                    recipient=user,
                    notification_type="grace_period_started",
                    title="Payment failed – grace period started",
                    message=f"Your payment of ${total_amount:.2f} failed. You have 3 days to update your payment method before services are suspended.",
                    invoice_id=invoice.id,
                )

        except Exception as e:
            logger.error(f"Critical error processing user {user_id}: {e}")


def enforce_expired_grace_periods():
    """
    Daily job to check for expired grace periods and take action.
    
    Actions:
    1. Mark invoice as 'failed'
    2. Flag all related advertisements and listings
    3. Suspend the business profile
    
    Returns:
        int: Number of invoices processed
    """
    from api.models import MonthlyInvoice, Advertisement, Listings
    
    logger.info("Starting grace period enforcement check...")
    
    # Find all invoices with expired grace periods
    now = timezone.now()
    expired_invoices = MonthlyInvoice.objects.filter(
        status='grace',
        grace_expires_at__lte=now
    ).select_related('user').prefetch_related('user__business_profile')
    
    enforced_count = 0
    
    for invoice in expired_invoices:
        try:
            with transaction.atomic():
                # 1. Mark invoice as failed
                invoice.status = 'failed'
                invoice.save(update_fields=['status', 'updated_at'])
                
                user = invoice.user
                
                # 2. Flag all user's advertisements
                ads_flagged = Advertisement.objects.filter(
                    user=user,
                    advertisement_status='active'
                ).update(
                    advertisement_status='flagged',
                    is_ever_flagged=True,
                    admin_actions='flagged'
                )
                
                # 3. Flag all user's listings
                listings_flagged = Listings.objects.filter(
                    user=user,
                    listing_status='active'
                ).update(
                    listing_status='flagged',
                    is_ever_flagged=True,
                    admin_actions='flagged'
                )
                
                # 4. Suspend business profile if exists
                business_suspended = False
                if hasattr(user, 'business_profile'):
                    business_profile = user.business_profile
                    business_profile.is_suspended = True
                    business_profile.save(update_fields=['is_suspended'])
                    business_suspended = True
                
                enforced_count += 1
                
                logger.warning(
                    f"Grace period expired for Invoice {invoice.id} ({user.email}). "
                    f"Ads flagged: {ads_flagged}, Listings flagged: {listings_flagged}, "
                    f"Business suspended: {business_suspended}"
                )
                
                # Notify user about suspension
                from api.notification_service import create_notification
                create_notification(
                    recipient=user,
                    notification_type="account_suspended",
                    title="Account suspended – grace period expired",
                    message="Your grace period has expired and your account has been suspended. Please settle your outstanding balance to restore services.",
                    invoice_id=invoice.id,
                )
                
        except Exception as e:
            logger.error(f"Error enforcing grace period for invoice {invoice.id}: {e}", exc_info=True)
    
    logger.info(f"Grace period enforcement completed. {enforced_count} invoice(s) processed.")
    return enforced_count


def apply_pending_discount_changes():
    """
    Every 30 mins: promotes pending discount changes to live.

    For each channel with a pending discount change:
      Checks if exactly `pending_discount_effective_at` UTC time has been reached.
      If so:
      1. Closes the current ChannelDiscountHistory entry (effective_until = yesterday local)
      2. Applies pending → live fields (apply_discount, discount_price)
      3. Opens a new ChannelDiscountHistory entry effective from today local
      4. Clears the pending fields
    """
    from api.models import AdvertisementChannel, ChannelDiscountHistory
    from datetime import timedelta
    from django.utils import timezone
    import zoneinfo

    now_utc = timezone.now()

    # Only pull channels that actually have a pending discount that has reached its effective UTC time.
    channels = AdvertisementChannel.objects.filter(
        pending_apply_discount__isnull=False,
        pending_discount_effective_at__isnull=False,
        pending_discount_effective_at__lte=now_utc
    )

    applied_count = 0

    for channel in channels:
        try:
            # 0. Calculate local dates based on the effective exact UTC time
            channel_tz = zoneinfo.ZoneInfo(channel.timezone or "America/Toronto")
            effective_local_date = channel.pending_discount_effective_at.astimezone(channel_tz).date()
            yesterday_local = effective_local_date - timedelta(days=1)

            # 1. Find the currently-active (open) ChannelDiscountHistory entry.
            open_entries = ChannelDiscountHistory.objects.filter(
                channel=channel,
                effective_until__isnull=True
            )
            if open_entries.exists():
                open_entries.update(effective_until=yesterday_local)
            else:
                if not ChannelDiscountHistory.objects.filter(channel=channel).exists():
                    ChannelDiscountHistory.objects.create(
                        channel=channel,
                        apply_discount=channel.apply_discount,  
                        discount_price=channel.discount_price,  
                        effective_from=channel.start_date,
                        effective_until=yesterday_local,
                    )

            # 2. Promote pending values → live
            channel.apply_discount = channel.pending_apply_discount
            channel.discount_price = channel.pending_discount_price or 0
            channel.discount_start_date = channel.pending_discount_start_date
            channel.discount_end_date = channel.pending_discount_end_date

            # 3. Open new history entry/entries for the new discount period
            # We use the same smart windowing logic as in create_advertisemen_channel
            if channel.apply_discount and channel.discount_start_date:
                # Gap: No discount from promotion day → day before discount start
                if channel.discount_start_date > effective_local_date:
                    ChannelDiscountHistory.objects.create(
                        channel=channel,
                        apply_discount=False,
                        discount_price=0,
                        effective_from=effective_local_date,
                        effective_until=channel.discount_start_date,
                    )

                # Window: Discount active during the specified window
                ChannelDiscountHistory.objects.create(
                    channel=channel,
                    apply_discount=True,
                    discount_price=channel.discount_price,
                    effective_from=max(effective_local_date, channel.discount_start_date),
                    effective_until=channel.discount_end_date,
                )

                # Post-window: If discount has an end date, seed no-discount entry after it
                if channel.discount_end_date:
                    day_after = channel.discount_end_date + timedelta(days=1)
                    if channel.end_date is None or day_after <= channel.end_date:
                        ChannelDiscountHistory.objects.create(
                            channel=channel,
                            apply_discount=False,
                            discount_price=0,
                            effective_from=day_after,
                        )
            else:
                # Standard case: immediate or no discount
                ChannelDiscountHistory.objects.create(
                    channel=channel,
                    apply_discount=channel.apply_discount,
                    discount_price=channel.discount_price,
                    effective_from=effective_local_date
                )

            # 4. Clear pending fields
            channel.pending_apply_discount = None
            channel.pending_discount_price = None
            channel.pending_discount_start_date = None
            channel.pending_discount_end_date = None
            channel.pending_discount_effective_at = None
            channel.save()

            applied_count += 1
            logger.info(
                f'Channel "{channel.channel_name}" (ID: {channel.id}): '
                f'discount promoted → apply_discount={channel.apply_discount}, '
                f'discount_price={channel.discount_price}'
            )
        except Exception as e:
            logger.error(
                f'Error promoting discount for channel {channel.id}: {e}',
                exc_info=True
            )

    logger.info(f"apply_pending_discount_changes: {applied_count} channel(s) updated.")
    return applied_count


def expire_channel_discounts():
    """
    Runs daily: finds active channels whose discount_end_date has passed
    and deactivates the discount.

    This handles the case where an admin sets a finite discount window
    (e.g., a 2-week promotional period). Once the end date passes, the
    discount is automatically cleared so ads are billed at full price.
    """
    from api.models import AdvertisementChannel, ChannelDiscountHistory

    today_utc = timezone.now().date()
    expired_count = 0

    channels = AdvertisementChannel.objects.filter(
        apply_discount=True,
        discount_end_date__isnull=False,
        discount_end_date__lt=today_utc,
        status='active',
    )

    for channel in channels:
        try:
            with transaction.atomic():
                # Close the current discount history period
                latest_history = channel.discount_history.filter(
                    effective_until__isnull=True
                ).first()
                if latest_history:
                    latest_history.effective_until = today_utc
                    latest_history.save(update_fields=['effective_until'])

                # Open a new history entry with no discount
                ChannelDiscountHistory.objects.create(
                    channel=channel,
                    apply_discount=False,
                    discount_price=0,
                    effective_from=today_utc,
                )

                # Reset the live discount fields
                channel.apply_discount = False
                channel.discount_price = 0
                channel.discount_start_date = None
                channel.discount_end_date = None
                channel.save(update_fields=[
                    'apply_discount', 'discount_price',
                    'discount_start_date', 'discount_end_date', 'updated_at',
                ])

                expired_count += 1
                logger.info(f"Discount expired for channel {channel.id} ({channel.channel_name})")

                # Notify: discount removed
                from api.notification_service import create_notification
                from api.models import AdvertisementChannelAssignment
                assignments = AdvertisementChannelAssignment.objects.filter(
                    advertisement_channel=channel,
                ).select_related("advertisement__user")
                notified_users = set()
                for assignment in assignments:
                    biz_user = assignment.advertisement.user
                    if biz_user.id in notified_users:
                        continue
                    notified_users.add(biz_user.id)
                    create_notification(
                        recipient=biz_user,
                        notification_type="discount_ended",
                        title=f'Discount ended on "{channel.channel_name}"',
                        message=f'The discount on channel "{channel.channel_name}" has expired. Ads are now billed at full price.',
                        channel_id=channel.id,
                        recipient_role="business",
                    )

                # Notify mobile users who liked this channel
                from api.models import LikedAdvertisementChannel
                from api.notification_service import create_bulk_notifications

                likes = LikedAdvertisementChannel.objects.filter(
                    advertisement_channel=channel
                ).select_related("client_profile__user")
                client_users = [like.client_profile.user for like in likes]
                if client_users:
                    create_bulk_notifications(
                        recipients=client_users,
                        notification_type="discount_ended",
                        title=f'Discount ended on "{channel.channel_name}"',
                        message=f'The discount on channel "{channel.channel_name}" has expired.',
                        recipient_role="user",
                        channel_id=channel.id,
                    )
                    logger.info(f"Notified {len(client_users)} mobile user(s) about discount expiry on channel {channel.id}")

        except Exception as e:
            logger.error(f"Error expiring discount for channel {channel.id}: {e}", exc_info=True)

    logger.info(f"expire_channel_discounts: {expired_count} channel(s) discount(s) cleared.")
    return expired_count


def send_phaseout_warnings():
    """
    Runs every 30 minutes. 
    Notifies businesses about channels AND ads expiring in exactly 11.5 to 12 hours.
    Rounding to nearest 30-minute block prevents duplicates or missed notifications 
    if the cron job runs slightly early or late.
    """
    from api.models import AdvertisementChannel, AdvertisementChannelAssignment, Advertisement, LikedAdvertisementChannel, LikedAdvertisement
    from api.notification_service import create_notification, create_bulk_notifications
    from datetime import timedelta

    now = timezone.now()
    
    # Round down to nearest 30 minutes (e.g. 12:00:05 -> 12:00:00)
    minute = (now.minute // 30) * 30
    base_time = now.replace(minute=minute, second=0, microsecond=0)

    window_start = base_time + timedelta(hours=11, minutes=30)
    window_end = base_time + timedelta(hours=12)

    # 1. 12-Hour Warnings for Channels
    expiring_channels = AdvertisementChannel.objects.filter(
        status="active",
        expiry_at__gt=window_start,
        expiry_at__lte=window_end,
    )
    
    channel_notified = 0
    for channel in expiring_channels:
        assignments = AdvertisementChannelAssignment.objects.filter(
            advertisement_channel=channel,
            advertisement__advertisement_status="active",
        ).select_related("advertisement__user")

        user_ads_map = {}
        for assignment in assignments:
            user = assignment.advertisement.user
            if user.id not in user_ads_map:
                user_ads_map[user.id] = {"user": user, "ads": []}
            user_ads_map[user.id]["ads"].append(assignment.advertisement)

        for user_id, info in user_ads_map.items():
            user = info["user"]
            ads = info["ads"]
            ad_names = ", ".join([f'"{ad.advertisment_name}"' for ad in ads])

            create_notification(
                recipient=user,
                notification_type="channel_phaseout",
                title=f'Channel "{channel.channel_name}" phasing out in 12 hours',
                message=(
                    f'The channel "{channel.channel_name}" will phase out in approximately 12 hours. '
                    f'Your active ad(s) on this channel will also be affected: {ad_names}.'
                ),
                channel_id=channel.id,
                recipient_role="business",
            )
            channel_notified += 1

        # Notify mobile users who liked this channel
        likes = LikedAdvertisementChannel.objects.filter(
            advertisement_channel=channel
        ).select_related("client_profile__user")
        client_users = [like.client_profile.user for like in likes]
        if client_users:
            create_bulk_notifications(
                recipients=client_users,
                notification_type="channel_phaseout",
                title=f'Channel "{channel.channel_name}" phasing out soon',
                message=f'The channel "{channel.channel_name}" you liked will phase out in approximately 12 hours.',
                recipient_role="user",
                channel_id=channel.id,
            )
            channel_notified += len(client_users)

    # 2. 12-Hour Warnings for Ads
    expiring_ads = Advertisement.objects.filter(
        advertisement_status="active",
        expiry_at__gt=window_start,
        expiry_at__lte=window_end,
    ).select_related("user")
    
    ad_notified = 0
    for ad in expiring_ads:
        create_notification(
            recipient=ad.user,
            notification_type="ad_phaseout_warning",
            title=f'Advertisement "{ad.advertisment_name}" phasing out in 12 hours',
            message=(f'Your advertisement "{ad.advertisment_name}" will expire and phase out in approximately 12 hours.'),
            advertisement_id=ad.id,
            recipient_role="business",
        )
        ad_notified += 1

        # Notify mobile users who liked this advertisement
        ad_likes = LikedAdvertisement.objects.filter(
            advertisement=ad
        ).select_related("client_profile__user")
        ad_client_users = [like.client_profile.user for like in ad_likes]
        if ad_client_users:
            create_bulk_notifications(
                recipients=ad_client_users,
                notification_type="ad_phaseout_warning",
                title=f'Advertisement "{ad.advertisment_name}" phasing out soon',
                message=f'The advertisement "{ad.advertisment_name}" you liked will phase out in approximately 12 hours.',
                recipient_role="user",
                advertisement_id=ad.id,
            )
            ad_notified += len(ad_client_users)

    logger.info(f"send_phaseout_warnings: {channel_notified} channel warning(s), {ad_notified} ad warning(s) sent.")
    return channel_notified + ad_notified

def notify_phased_out_ads(ad_ids):
    """
    Sends 'ad_phased_out' notifications to owners of the specified ads.
    """
    if not ad_ids:
        return
    
    from api.models import Advertisement
    from api.notification_service import create_notification
    
    ads = Advertisement.objects.filter(id__in=ad_ids).select_related('user')
    count = 0
    for ad in ads:
        try:
            create_notification(
                recipient=ad.user,
                notification_type="ad_phased_out",
                title=f'Advertisement "{ad.advertisment_name}" phased out',
                message=f'Your advertisement "{ad.advertisment_name}" has been phased out because its end date was reached or its last active channel expired.',
                advertisement_id=ad.id,
            )
            count += 1
        except Exception as e:
            logger.error(f"Failed to notify phaseout for ad {ad.id}: {e}")
            
    logger.info(f"notify_phased_out_ads: sent {count} notification(s).")


def send_discount_expiry_reminders():
    """
    Daily job: notifies businesses whose channel discount ends tomorrow.
    Runs at 06:00 UTC alongside phase-out alerts.
    """
    from api.models import AdvertisementChannel, AdvertisementChannelAssignment
    from api.notification_service import create_notification
    from datetime import timedelta

    tomorrow = timezone.now().date() + timedelta(days=1)

    channels = AdvertisementChannel.objects.filter(
        apply_discount=True,
        discount_end_date=tomorrow,
        status="active",
    )

    notified = 0
    for channel in channels:
        assignments = AdvertisementChannelAssignment.objects.filter(
            advertisement_channel=channel,
        ).select_related("advertisement__user")

        notified_users = set()
        for assignment in assignments:
            user = assignment.advertisement.user
            if user.id in notified_users:
                continue
            notified_users.add(user.id)

            create_notification(
                recipient=user,
                notification_type="discount_ended",
                title=f'Discount ending tomorrow on "{channel.channel_name}"',
                message=f'The discount on channel "{channel.channel_name}" ends tomorrow ({tomorrow}). Ads will return to full price.',
                channel_id=channel.id,
                recipient_role="business",
            )
            notified += 1

        # Notify mobile users who liked this channel (12hr heads-up)
        from api.models import LikedAdvertisementChannel
        from api.notification_service import create_bulk_notifications

        likes = LikedAdvertisementChannel.objects.filter(
            advertisement_channel=channel
        ).select_related("client_profile__user")
        client_users = [like.client_profile.user for like in likes]
        if client_users:
            create_bulk_notifications(
                recipients=client_users,
                notification_type="discount_ending",
                title=f'Discount ending soon on "{channel.channel_name}"',
                message=f'The discount on channel "{channel.channel_name}" ends tomorrow ({tomorrow}). Don\'t miss out!',
                recipient_role="user",
                channel_id=channel.id,
            )
            notified += len(client_users)
            logger.info(f"Notified {len(client_users)} mobile user(s) about discount ending on channel {channel.id}")

    logger.info(f"send_discount_expiry_reminders: {notified} reminder(s) sent for {channels.count()} channel(s).")
    return notified