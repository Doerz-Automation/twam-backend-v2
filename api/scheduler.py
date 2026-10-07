from apscheduler.schedulers.background import BackgroundScheduler
from django.db import connection
from django.utils import timezone
import atexit
import logging

logger = logging.getLogger(__name__)


def _expire_channels():
    """
    Single bulk SQL UPDATE — moves active channels to 'phaseout' when their
    end_date has passed in their own IANA timezone.

    Uses Postgres AT TIME ZONE per-row so no Python loop and no hardcoded
    timezone list are needed. The partial index idx_channel_active_enddate
    (end_date WHERE status='active') prevents a full table scan.
    """
    with connection.cursor() as cursor:
        cursor.execute("""
            UPDATE api_advertisementchannel
            SET    status     = 'phaseout',
                   updated_at = NOW()
            WHERE  status     = 'active'
              AND  expiry_at  IS NOT NULL
              AND  expiry_at  <= NOW()
        """)
        count = cursor.rowcount
    logger.info(f"_expire_channels: {count} channel(s) → 'phaseout'")
    return count


def _expire_ads():
    """
    Two bulk SQL UPDATEs — no Python loop.

    Bulk #1 — Ad's own end_date has passed (UTC, aligns with billing cutoff).
        Billing uses ad.end_date as a plain date; we keep this timezone-neutral
        to avoid discrepancies between what billing charged and when the ad ran.

    Bulk #2 — Ad has no remaining active channels.
        Uses NOT EXISTS so the ad stays alive as long as even ONE channel is
        still active. An ad linked to 3 channels where only 1 expires stays
        active on the remaining 2 — matching the existing multi-channel behaviour.
    """
    with connection.cursor() as cursor:

        # Bulk #1: end_date expiry (timezone-neutral — matches billing)
        cursor.execute("""
            UPDATE api_advertisement
            SET    advertisement_status = 'phaseout',
                   updated_at           = NOW()
            WHERE  advertisement_status = 'active'
              AND  expiry_at            IS NOT NULL
              AND  expiry_at            <= NOW()
        """)
        end_date_count = cursor.rowcount
        logger.info(f"_expire_ads (end_date): {end_date_count} ad(s) → 'phaseout'")

        # Bulk #2: no active channels remaining
        cursor.execute("""
            UPDATE api_advertisement ad
            SET    advertisement_status = 'phaseout',
                   updated_at           = NOW()
            WHERE  ad.advertisement_status = 'active'
              AND  NOT EXISTS (
                  SELECT 1
                  FROM   api_advertisementchannelassignment aca
                  JOIN   api_advertisementchannel ch
                         ON ch.id = aca.advertisement_channel_id
                  WHERE  aca.advertisement_id = ad.id
                    AND  ch.status            = 'active'
              )
        """)
        no_channel_count = cursor.rowcount
        logger.info(f"_expire_ads (no active channel): {no_channel_count} ad(s) → 'phaseout'")

    return end_date_count, no_channel_count


def update_channel_status():
    """
    Daily status update job.

    Execution order matters:
      1. Expire channels first (so their status is 'phaseout' before step 2)
      2. Then identify ads that will be phased out (either by date or because 
         they have no active channels remaining)
      3. Perform bulk ad phaseout
      4. Notify owners of phased-out ads
    """
    try:
        from api.tasks import notify_phased_out_ads
        
        # 1. Expire channels first
        ch_count = _expire_channels()
        
        # 2. Identify ads that are ACTIVE but will be phased out
        # (Must be done before _expire_ads bulk UPDATE to catch the IDs)
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT id 
                FROM   api_advertisement ad
                WHERE  ad.advertisement_status = 'active'
                  AND  (
                      (expiry_at IS NOT NULL AND expiry_at <= NOW())
                      OR
                      NOT EXISTS (
                          SELECT 1
                          FROM   api_advertisementchannelassignment aca
                          JOIN   api_advertisementchannel ch
                                 ON ch.id = aca.advertisement_channel_id
                          WHERE  aca.advertisement_id = ad.id
                            AND  ch.status            = 'active'
                      )
                  )
            """)
            ad_ids = [row[0] for row in cursor.fetchall()]

        # 3. Run bulk UPDATEs for ads
        ad_end_count, ad_no_ch_count = _expire_ads()
        
        logger.info(
            f"Status update complete → "
            f"channels: {ch_count} | "
            f"ads by end_date: {ad_end_count} | "
            f"ads by no-channel: {ad_no_ch_count}"
        )

        # 4. Notify businesses
        if ad_ids:
            notify_phased_out_ads(ad_ids)

    except Exception as e:
        logger.error(f"Error in update_channel_status: {e}", exc_info=True)


def start_scheduler():

    scheduler = BackgroundScheduler(timezone="UTC")

    # Runs at 19:00 UTC = midnight ET (UTC-5) — earliest Canadian timezone to cross midnight
    # Channels in later timezones (e.g. PT, UTC-8) are evaluated correctly because
    # the SQL uses AT TIME ZONE per-row; re-running earlier simply evaluates those
    # channels and finds they haven't expired yet (no false positives).
    scheduler.add_job(
        update_channel_status,
        'cron',
        minute='0,30', # Runs every hour on the hour and half-hour
        id='channel_status_update',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        name='Update Channel & Ad Status (UTC Exact Expiry)'
    )

    # Monthly Billing Job: Runs at 00:00 UTC on the 1st of every month
    from api.tasks import run_monthly_billing
    scheduler.add_job(
        run_monthly_billing,
        'cron',
        day='1',
        hour=0,
        minute=0,
        id='monthly_billing_job',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        name='Run Monthly Billing (1st of Month)'
    )

    # Grace Period Enforcement: Runs daily at 00:05 UTC
    from api.tasks import enforce_expired_grace_periods
    scheduler.add_job(
        enforce_expired_grace_periods,
        'cron',
        hour=0,
        minute=5,
        id='grace_period_enforcement',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        name='Enforce Expired Grace Periods (Daily)'
    )

    # Pending Discount Activation: Runs every 30 mins to catch local midnights globally
    from api.tasks import apply_pending_discount_changes
    scheduler.add_job(
        apply_pending_discount_changes,
        'cron',
        minute='0,30',
        id='apply_pending_discounts',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        name='Apply Pending Discount Changes'
    )

    # Discount Expiry: Runs daily at 00:10 UTC to deactivate expired discount windows
    from api.tasks import expire_channel_discounts
    scheduler.add_job(
        expire_channel_discounts,
        'cron',
        hour=0,
        minute=10,
        id='expire_channel_discounts',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        name='Expire Channel Discounts (Daily)'
    )

    # Phase-out Warnings: Runs every 30 mins 
    # Notifies businesses about channels and ads expiring in EXACTLY 11.5 - 12 hours
    from api.tasks import send_phaseout_warnings
    scheduler.add_job(
        send_phaseout_warnings,
        'cron',
        minute='0,30',
        id='phaseout_warnings',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        name='Phase-out 12-Hour Warnings'
    )

    # Discount Expiry Reminders: Runs daily at 06:05 UTC
    # Notifies businesses when a channel's discount ends tomorrow
    from api.tasks import send_discount_expiry_reminders
    scheduler.add_job(
        send_discount_expiry_reminders,
        'cron',
        hour=6,
        minute=5,
        id='discount_expiry_reminders',
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        name='Discount Expiry Reminders (Daily)'
    )

    try:
        scheduler.start()
        logger.info('=' * 60)
        logger.info('APScheduler started successfully')
        logger.info('Scheduled jobs:')
        for job in scheduler.get_jobs():
            logger.info(f'  - {job.name} (ID: {job.id}) - Next run: {job.next_run_time}')
        logger.info('=' * 60)
    except Exception as e:
        logger.error(f'Failed to start APScheduler: {str(e)}')

    # Run immediately on startup — catches any channels/ads that expired while
    # the server was down
    try:
        logger.info('Running startup status check...')
        update_channel_status()
        logger.info('Startup status check complete.')
    except Exception as e:
        logger.error(f'Startup update_channel_status failed: {str(e)}', exc_info=True)

    # Apply any pending discounts that are due at startup
    try:
        from api.tasks import apply_pending_discount_changes
        logger.info('Running startup pending discount check...')
        apply_pending_discount_changes()
        logger.info('Startup pending discount check complete.')
    except Exception as e:
        logger.error(f'Startup apply_pending_discount_changes failed: {str(e)}', exc_info=True)

    # Expire any discounts that passed their end date while server was down
    try:
        from api.tasks import expire_channel_discounts
        logger.info('Running startup discount expiry check...')
        expire_channel_discounts()
        logger.info('Startup discount expiry check complete.')
    except Exception as e:
        logger.error(f'Startup expire_channel_discounts failed: {str(e)}', exc_info=True)

    # Shutdown scheduler gracefully on app exit
    atexit.register(lambda: scheduler.shutdown() if scheduler.running else None)


def trigger_channel_status_update():
    logger.info('Manually triggering channel status update')
    update_channel_status()

def trigger_channel_status_update():
    logger.info('Manually triggering channel status update')
    update_channel_status()
