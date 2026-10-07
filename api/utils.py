from django.utils.timezone import now
from .models import Listings, models, User, AdvertisementFlag, Advertisement
from rest_framework_simplejwt.tokens import RefreshToken
from twam_api.settings import (
    API_SECRET_KEY, OTP_RESEND_INTERVAL, OTP_MAX_RETRIES, OTP_COOLDOWN_PERIOD,
    AWS_SES_VERIFIED_SENDER, AWS_REGION_NAME, AWS_SES_ACCESS_KEY, AWS_SES_SECRET_KEY
)

from api.types import AdvertisementReturnType, AdvertisementFlagType, AdvertisementChannelType, BusinessProfileReturnType, ChannelAdvertisementReturnType, ListingFlagType, ListingType
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
from django.utils import timezone as notTimeZone
from django.db.models import Q
from django.apps import apps
from graphql import GraphQLError
from typing import Optional
import random
import os
import logging
import boto3
from botocore.exceptions import ClientError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import COMMASPACE, formatdate
from os.path import basename, isfile
logger = logging.getLogger(__name__)


def calculate_flagged_days(ad, start_date, end_date):
    """
    Calculates the number of days the ad was flagged during the period [start_date, end_date].
    """
    from datetime import datetime, timedelta, timezone as dt_timezone
    start_dt = datetime.combine(
        start_date, datetime.min.time()).replace(tzinfo=dt_timezone.utc)
    end_dt = datetime.combine(
        end_date, datetime.max.time()).replace(tzinfo=dt_timezone.utc)

    # Fetch overlapping flags
    flags = ad.flags.filter(
        flagged_at__lte=end_dt
    ).filter(
        Q(resolved_at__gte=start_dt) | Q(resolved_at__isnull=True)
    )

    flagged_dates = set()

    for flag in flags:
        # Determine the effective start and end dates of the flag within the billing period
        flag_start = flag.flagged_at.date()
        if flag_start < start_date:
            flag_start = start_date

        flag_end = end_date
        if flag.resolved and flag.resolved_at:
            # resolved_at is the moment the flag was cleared — the ad is active again
            # from that calendar day onward, so the last FLAGGED day is resolved_at.date() - 1.
            from datetime import timedelta as _td
            flag_end_date = flag.resolved_at.date() - _td(days=1)
            if flag_end_date < end_date:
                flag_end = flag_end_date

        # Add days to set
        current = flag_start
        while current <= flag_end:
            flagged_dates.add(current)
            current += timedelta(days=1)

    return flagged_dates  # returns a set of date objects


def build_discount_history(channel):
    """
    Returns a list of ChannelDiscountHistoryType for an AdvertisementChannel,
    ordered chronologically. Reused by every resolver that exposes channel data.
    """
    from api.types import ChannelDiscountHistoryType
    return [
        ChannelDiscountHistoryType(
            id=h.id,
            apply_discount=h.apply_discount,
            discount_price=float(h.discount_price) if h.discount_price is not None else None,
            effective_from=h.effective_from,
            effective_until=h.effective_until,
        )
        for h in channel.discount_history.order_by("effective_from")
    ]


class PasswordResetToken(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    token = models.CharField(max_length=50, unique=True)
    expires_at = models.DateTimeField()

    def is_valid(self):
        return now() < self.expires_at


class SESEmailSender:
    def __init__(self):
        """
        Initialize with settings from Django settings.py
        """
        
 
        self.region_name = AWS_REGION_NAME
        self.access_key = AWS_SES_ACCESS_KEY
        self.secret_key = AWS_SES_SECRET_KEY
        self.from_email = AWS_SES_VERIFIED_SENDER

        # Validate required settings
        if not all([self.access_key, self.secret_key, self.from_email]):
            raise ValueError(
                "Missing required AWS SES configuration in settings")

        # Initialize boto3 client
        from botocore.config import Config
        aws_config = Config(
            connect_timeout=3,
            read_timeout=3,
            retries={'max_attempts': 1}
        )
        self.client = boto3.client(
            'ses',
            region_name=self.region_name,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            config=aws_config
        )

    def send_email(
        self,
        recipient: str,
        subject: str,
        body_text: str = None,
        body_html: str = None,
        cc_recipients: list = None,
        bcc_recipients: list = None
    ) -> bool:
        """
        Send email via AWS SES API

        Args:
            recipient: Primary recipient email address
            subject: Email subject
            body_text: Plain text email content
            body_html: HTML email content
            cc_recipients: List of CC email addresses
            bcc_recipients: List of BCC email addresses

        Returns:
            bool: True if email sent successfully
        """
        # Prepare destination addresses
        destination = {'ToAddresses': [recipient]}
        if cc_recipients:
            destination['CcAddresses'] = cc_recipients
        if bcc_recipients:
            destination['BccAddresses'] = bcc_recipients

        # Prepare message body
        message = {'Subject': {'Data': subject}}
        if body_text and body_html:
            message['Body'] = {
                'Text': {'Data': body_text},
                'Html': {'Data': body_html}
            }
        elif body_text:
            message['Body'] = {'Text': {'Data': body_text}}
        else:
            message['Body'] = {'Html': {'Data': body_html}}

        try:
            response = self.client.send_email(
                Source=self.from_email,
                Destination=destination,
                Message=message
            )
            
            print("response",response)
            logger.info(
                f"Email sent to {recipient} with message ID: {response['MessageId']}")
            return True
        except ClientError as e:
            logger.error(
                f"AWS SES error sending email: {e.response['Error']['Message']}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending email: {str(e)}")
            return False


def generate_jwt_token(user):
    """Generate JWT access and refresh tokens for the user."""
    refresh = RefreshToken.for_user(user)

    refresh["id"] = str(user.id)  # User ID as string
    refresh["email"] = user.email
    refresh["role"] = user.role
    refresh["exp"] = datetime.now(
        timezone.utc) + timedelta(days=7)  # Token expiry in 7 days
    refresh["iat"] = datetime.now(timezone.utc)  # Issued at time

    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }


def validate_api_secret(info) -> None:
    """
    Validates the API secret from the request headers.
    Raises an exception if the secret is invalid or missing.
    """
    request = info.context["request"]
    secret_key = request.headers.get('X-API-SECRET')

    if secret_key != API_SECRET_KEY:
        raise Exception("Invalid or missing API secret")


def generate_report_id():
    return str(random.randint(10000000, 99999999))


def generate_and_send_otp(user, purpose="Authentication", new_email=False, expiry_minutes=5):
    """
    Generates a new OTP for a given user and sends it to their email.
    - Checks resend interval and cooldown.
    - Sends OTP to email.

    Parameters:
    - user (User): The user object to send the OTP to.
    - purpose (str): Reason for the OTP (e.g., 'Signup', 'Forgot Password').
    - expiry_minutes (int): Expiry duration of the OTP in minutes (default: 5).

    Returns:
    - str: Success message or raises an error.
    """
    now = datetime.now(timezone.utc)
    OTP = apps.get_model('api', 'OTP')

    # Retrieve the most recent OTP record, if available
    otp_record = OTP.objects.filter(user=user).order_by('-created_at').first()

    if otp_record:
        # Calculate time since the last OTP was sent
        time_since_last_sent = (now - otp_record.last_sent_at).total_seconds()

        # Enforce cooldown if max retries are exceeded
        print(f"retry_count=======>{otp_record.retry_count}")
        print(f"OTP_MAX_RETRIES======>{OTP_MAX_RETRIES}")
        if otp_record.retry_count >= OTP_MAX_RETRIES:
            print(f"time_since_last_sent=======>{time_since_last_sent}")
            cooldown_remaining = OTP_COOLDOWN_PERIOD - time_since_last_sent
            if cooldown_remaining > 0:
                minutes = int(cooldown_remaining // 60)
                raise GraphQLError(
                    f"Too many OTP requests. Please try again in {minutes} minutes.")

        # Enforce minimum resend interval

        if time_since_last_sent < OTP_RESEND_INTERVAL:
            wait_time = OTP_RESEND_INTERVAL - time_since_last_sent
            raise GraphQLError(
                f"Please wait {int(wait_time)} seconds before requesting another OTP.")

        # If cooldown has passed, reset retry count
        if time_since_last_sent >= OTP_COOLDOWN_PERIOD:
            otp_record.retry_count = 0

        # Generate a new OTP, update fields, and save
        otp_record.otp_code = f"{random.randint(100000, 999999)}"
        otp_record.expires_at = now + \
            timedelta(minutes=expiry_minutes)  # Reset expiration
        otp_record.retry_count += 1
        otp_record.last_sent_at = now
        otp_record.verification_attempts = 0
        otp_record.save()
    else:
        # Create a new OTP record if none exists
        otp_code = f"{random.randint(100000, 999999)}"
        otp_record = OTP.objects.create(
            user=user,
            otp_code=otp_code,
            expires_at=now + timedelta(minutes=expiry_minutes),
            retry_count=1,
            last_sent_at=now,
            verification_attempts=0
        )

    # Send the OTP email
    full_name = f"{user.first_name} {user.last_name}"

    if new_email:
        send_otp_email(new_email, full_name, otp_record.otp_code, purpose)
    else:
        send_otp_email(user.email, full_name, otp_record.otp_code, purpose)

    return "OTP sent successfully."


def send_otp_email(user_email, full_name, otp_code, purpose="Authentication"):
    """
    Sends an OTP email with a customized message based on the purpose.

    Parameters:
    - user_email (str): Recipient's email address.
    - full_name (str): Recipient's full name.
    - otp_code (str): The OTP code.
    - purpose (str): Purpose of the OTP (default: "Authentication").
    """

    logger.debug(f"Sending OTP to: {user_email}")
    logger.debug(f"Recipient Name: {full_name}")
    logger.debug(f"OTP Code: {otp_code}")

    # Initialize sender
    email_sender = SESEmailSender()

    # Custom email content based on purpose
    subject = "Your OTP Code"
    plain_text = f"Hello {full_name},\n\nYour OTP code is {otp_code}. It is valid for 5 minutes."
    html_text = f"<html><body>Hello {full_name},<br>Your OTP for {purpose} is <strong>{otp_code}</strong>. It is valid for 5 minutes.</body></html>"

    logger.debug(f"Subject: {subject}")
    logger.debug(f"Plain Text: {plain_text}")

    sender = SESEmailSender()

    return sender.send_email(
        recipient=user_email,
        subject=subject,
        body_text=plain_text,
        body_html=html_text
    )

# helper function for queries


def attach_flag(ad_data: AdvertisementReturnType, flag: Optional[AdvertisementFlag]) -> AdvertisementReturnType:
    if flag:
        ad_data._flag_data = AdvertisementFlagType(
            title=flag.title,
            description=flag.description,
            images=flag.images,
            flagged_at=flag.flagged_at,
            resolved=flag.resolved,
        )
    return ad_data


def build_flag_fields(ad: Advertisement) -> tuple[bool, Optional[AdvertisementFlagType]]:
    # Use the property active_flag instead of ad.flag directly
    if ad.active_flag:
        flag = ad.active_flag
        return (
            not flag.resolved,
            AdvertisementFlagType(
                id=flag.id,
                title=flag.title,
                description=flag.description,
                images=flag.images or [],
                flagged_at=flag.flagged_at,
                resolved=flag.resolved,
            )
        )
    return False, None


def build_listing_flag_fields(listing: Listings) -> tuple[bool, Optional[ListingFlagType]]:
    if hasattr(listing, "flag") and listing.flag is not None:
        flag = listing.flag
        return (
            not flag.resolved,  # ✅ True if still flagged
            ListingFlagType(
                id=flag.id,
                title=flag.title,
                description=flag.description,
                images=flag.images or [],
                flagged_at=flag.flagged_at,
                resolved=flag.resolved,
            )
        )
    return False, None


def build_advertisement_response(ad: Advertisement) -> AdvertisementReturnType:
    is_flagged, flag_obj = build_flag_fields(ad)
    business_profile = getattr(ad.user, "business_profile", None)

    business_profile_data = None
    if business_profile:
        business_profile_data = BusinessProfileReturnType(
            id=business_profile.id,
            business_name=business_profile.business_name,
            description=business_profile.description,
            logo_url=business_profile.logo_url,
            cover_image=business_profile.cover_image,
            address=business_profile.address,
            address2=business_profile.address2,
            city=business_profile.city,
            state=business_profile.state,
            zip_code=business_profile.zip_code,
            latitude=business_profile.latitude,
            longitude=business_profile.longitude,
            phone=business_profile.phone,
            website=business_profile.website,
            payment_methods=business_profile.payment_methods or [],
            category=business_profile.category.name if business_profile.category else None,
            subcategories=business_profile.subcategories or [],
            is_suspended=business_profile.is_suspended

        )

    today = date.today()
    duration_end = ad.end_date if ad.end_date else today
    actual_cost = Decimal('0.00')
    if ad.start_date <= duration_end:
        flagged_date_set = calculate_flagged_days(ad, ad.start_date, duration_end)
        current_day = ad.start_date
        while current_day <= duration_end:
            if current_day not in flagged_date_set:
                actual_cost += ad.get_daily_rate(for_date=current_day)
            current_day += timedelta(days=1)

    return AdvertisementReturnType(
        id=ad.id,
        advertisment_name=ad.advertisment_name,
        advertisment_description=ad.advertisment_description,
        advertisement_image=ad.advertisement_image,
        start_date=ad.start_date,
        end_date=ad.end_date,
        advertisment_cost=float(ad.advertisment_cost),
        advertisement_status=ad.advertisement_status,
        total_revenue=float(ad.invoice_line_items.filter(invoice__status='paid').aggregate(models.Sum('amount'))['amount__sum'] or 0),
        actual_cost=actual_cost,
        is_ever_flagged=ad.is_ever_flagged,
        channel_assignments=[
            AdvertisementChannelType(
                id=assignment.advertisement_channel.id,
                channel_name=assignment.advertisement_channel.channel_name,
                city=assignment.advertisement_channel.city,
                province=assignment.advertisement_channel.province,
                locality=assignment.advertisement_channel.locality,
                latitude=assignment.advertisement_channel.latitude,
                longitude=assignment.advertisement_channel.longitude,
                price_per_day=assignment.advertisement_channel.price_per_day,
                apply_discount=assignment.advertisement_channel.is_discount_active_today(),
                discount_status_message=assignment.advertisement_channel.get_discount_status_message(),
                discount_price=assignment.advertisement_channel.discount_price,
                start_date=assignment.advertisement_channel.start_date,
                end_date=assignment.advertisement_channel.end_date,
                channel_image=assignment.advertisement_channel.channel_image,
                discount_history=build_discount_history(assignment.advertisement_channel),
            )
            for assignment in ad.channel_assignments.all()
        ],
        is_flagged=is_flagged,
        flag=flag_obj,
        business_profile=business_profile_data,
        is_reported=ad.is_reported,
        admin_actions=ad.admin_actions,
        stripe_subscription_id=ad.stripe_subscription_id,
        stripe_customer_id=ad.stripe_customer_id,
        last_billed_at=ad.last_billed_at,
        billing_status=ad.billing_status,
        has_been_reused=ad.reused_to.exists(),
    )


def build_channel_ad_response(ad: Advertisement) -> ChannelAdvertisementReturnType:

    is_flagged, flag_obj = build_flag_fields(ad)

    return ChannelAdvertisementReturnType(
        id=ad.id,
        advertisment_name=ad.advertisment_name,
        advertisment_description=ad.advertisment_description,
        advertisement_image=ad.advertisement_image,
        start_date=ad.start_date,
        end_date=ad.end_date,
        advertisment_cost=float(ad.advertisment_cost),
        advertisement_status=ad.advertisement_status,
        is_flagged=is_flagged,
        flag=flag_obj,
        is_ever_flagged=ad.is_ever_flagged,
        is_reported=ad.is_reported,
        has_been_reused=ad.reused_to.exists(),
    )


def build_listing_response(listing: Listings) -> ListingType:
    is_flagged, flag_obj = build_listing_flag_fields(listing)
    business_profile = getattr(listing.user, "business_profile", None)

    business_profile_data = None
    if business_profile:
        business_profile_data = BusinessProfileReturnType(
            id=business_profile.id,
            business_name=business_profile.business_name,
            description=business_profile.description,
            logo_url=business_profile.logo_url,
            cover_image=business_profile.cover_image,
            address=business_profile.address,
            address2=business_profile.address2,
            city=business_profile.city,
            state=business_profile.state,
            zip_code=business_profile.zip_code,
            latitude=business_profile.latitude,
            longitude=business_profile.longitude,
            phone=business_profile.phone,
            website=business_profile.website,
            payment_methods=business_profile.payment_methods or [],
            category=business_profile.category.name if business_profile.category else None,
            subcategories=business_profile.subcategories or [],
            created_at=business_profile.created_at,
            is_suspended=business_profile.is_suspended
        )

    return ListingType(
        id=listing.id,
        listing_name=listing.listing_name,
        listing_description=listing.listing_description,
        listing_image=listing.listing_image,
        listing_status=listing.listing_status,
        is_ever_flagged=listing.is_ever_flagged,
        created_at=listing.created_at,
        updated_at=listing.updated_at,
        business_profile=business_profile_data,
        flag=flag_obj,
        is_reported=listing.is_reported,is_flagged=is_flagged,
    )
