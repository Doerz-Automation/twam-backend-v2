from datetime import date
import uuid
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin, User
from django.db import models
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib.postgres.fields import ArrayField
from django.conf import settings


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        extra_fields.setdefault("is_active", True)  # Default users to active
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    ROLE_CHOICES = [
        ("admin", "Admin"),
        ("user", "User"),
        ("business", "business"),
    ]

    SUBSCRIPTION_CHOICES = [
        ("free", "Free"),
        ("paid", "Paid"),
    ]

    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=50)
    last_name = models.CharField(max_length=50)
    is_active = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    is_admin_approved = models.BooleanField(
        default=False, blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    role = models.CharField(
        max_length=10, choices=ROLE_CHOICES, default="user")
    subscription = models.CharField(
        max_length=10, choices=SUBSCRIPTION_CHOICES, null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)
    # Account holder's own phone — distinct from BusinessProfile.phone, which
    # belongs to the business, not the person.
    personal_phone = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(
        default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(
        default=timezone.now, null=True, blank=True)
    session_salt = models.UUIDField(default=uuid.uuid4, editable=False)

    objects = UserManager()

    class Meta:
        db_table = "api_user"  # Explicitly define the table name to avoid confusion

    def __str__(self):
        return self.email

    def is_admin(self):
        return self.role == "admin"

    def is_business(self):
        return self.role == "business"

    def is_paid_user(self):
        return self.role == "user" and self.subscription == "paid"


class Profile(models.Model):
    GENDER_CHOICES = [
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
    ]

    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="profile")
    gender = models.CharField(
        max_length=10, choices=GENDER_CHOICES, null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(
        default=timezone.now, null=True, blank=True)
    updated_at = models.DateTimeField(
        default=timezone.now, null=True, blank=True)


class OTP(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    otp_code = models.CharField(max_length=6)  # 6-digit OTP
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_sent_at = models.DateTimeField(
        auto_now=True)  # Tracks last sent timestamp
    retry_count = models.IntegerField(default=0)        # Counts OTP requests
    verification_attempts = models.IntegerField(default=0) # Counts incorrect verifications

    def is_expired(self):
        # Use timezone-aware datetime comparison
        return timezone.now() >= self.expires_at


class PaymentOtpAttempt(models.Model):
    """
    Tracks failed OTP attempts specifically for the payment method update flow.
    Kept separate from the general OTP model so that resending an OTP
    does NOT reset the attempt counter.
    """
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="payment_otp_attempt"
    )
    attempt_count = models.IntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"PaymentOtpAttempt: {self.user.email} — {self.attempt_count} attempt(s)"


class PasswordOtpAttempt(models.Model):
    """
    Tracks failed OTP attempts specifically for the password update flow.
    """
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="password_otp_attempt"
    )
    attempt_count = models.IntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"PasswordOtpAttempt: {self.user.email} — {self.attempt_count} attempt(s)"


class SettingsAccess(models.Model):
    """
    Server-side authorization for the whole Business Portal Settings area.

    A single successful OTP verification sets `verified_until` to a fixed
    10 minutes from that moment. The window is never extended by activity.
    It is bound to the access token (`verified_jti`) it was granted for, so a
    new login or a different token never inherits it.

    Failed OTP attempts are tracked here too, separately from the shared OTP
    model, so that resending an OTP does not reset the counter.
    """
    SESSION_MINUTES = 10

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="settings_access"
    )
    attempt_count = models.IntegerField(default=0)
    locked_at = models.DateTimeField(null=True, blank=True)
    verified_until = models.DateTimeField(null=True, blank=True)
    verified_jti = models.CharField(max_length=64, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def is_valid_for(self, jti):
        return bool(
            jti
            and self.verified_jti == jti
            and self.verified_until
            and timezone.now() < self.verified_until
        )

    def clear_session(self):
        self.verified_until = None
        self.verified_jti = ""
        self.save(update_fields=["verified_until", "verified_jti", "updated_at"])

    def __str__(self):
        return f"SettingsAccess: {self.user.email} — until {self.verified_until}"



# class BusinessCategory(models.Model):
#     name = models.CharField(max_length=100, unique=True)
#     subcategory = ArrayField(
#         models.CharField(max_length=50),
#         blank=True,
#         default=list,
#         help_text="List of tag strings for this category"
#     )

#     def __str__(self):
#         return self.name


class BusinessCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class BusinessSubCategory(models.Model):
    category = models.ForeignKey(
        BusinessCategory, related_name="subcategories", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(
        default=timezone.now, null=True, blank=True)

    class Meta:
        unique_together = ("category", "name")

    def __str__(self):
        return f"{self.category.name} - {self.name}"


class BusinessProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='business_profile')
    business_uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text="Permanent unique identifier assigned at business registration."
    )
    business_name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    logo_url = models.URLField(blank=True)
    cover_image = models.URLField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # either suspended by admin or not
    is_suspended = models.BooleanField(default=False)
    # Deactivation (permanent — business must re-register)
    is_deactivated = models.BooleanField(default=False)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    # Location fields
    address = models.TextField(blank=True)
    address2 = models.TextField(blank=True)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=100)
    zip_code = models.CharField(max_length=20)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    timezone_id = models.CharField(max_length=50, default='America/Toronto', help_text="User's local timezone (e.g. America/Toronto)")

    # Contact info
    phone = models.CharField(max_length=20)
    website = models.URLField(blank=True)

    # Payment methods
    payment_methods = ArrayField(
        models.CharField(max_length=20),
        blank=True,
        default=list
    )

    category = models.ForeignKey(
        BusinessCategory,
        on_delete=models.SET_NULL,
        null=True,
        related_name="business_profiles"
    )

    subcategories = ArrayField(
        models.CharField(max_length=50),
        blank=True,
        default=list
    )

    class Meta:
        verbose_name = "Business Profile"
        verbose_name_plural = "Business Profiles"

    def __str__(self):
        return self.business_name


class BusinessHours(models.Model):
    DAY_CHOICES = [
        ('monday', 'Monday'),
        ('tuesday', 'Tuesday'),
        ('wednesday', 'Wednesday'),
        ('thursday', 'Thursday'),
        ('friday', 'Friday'),
        ('saturday', 'Saturday'),
        ('sunday', 'Sunday')
    ]

    business = models.ForeignKey(
        BusinessProfile,
        on_delete=models.CASCADE,
        related_name='business_hours'
    )

    day = models.CharField(max_length=10, choices=DAY_CHOICES)
    opening_time = models.TimeField(null=True, blank=True)
    closing_time = models.TimeField(null=True, blank=True)
    is_closed = models.BooleanField(default=False)
    is_24hours=models.BooleanField(default=False)
    is_overnight=models.BooleanField(default=False)
    class Meta:
        verbose_name = "Business Hour"
        verbose_name_plural = "Business Hours"
        unique_together = ('business', 'day')

    def __str__(self):
        return f"{self.get_day_display()} - {self.business.business_name}"


class AdvertisementCategory(models.Model):
    advertisement = models.ForeignKey(
        "AdvertisementChannel", on_delete=models.CASCADE, related_name="advertisement_category")
    category = models.ForeignKey(
        "BusinessCategory", on_delete=models.CASCADE, related_name="category_advertisements")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class AdvertisementChannel(models.Model):

    channel_name = models.CharField(max_length=255)
    business_categories = models.ManyToManyField(
        "BusinessCategory",
        through="AdvertisementCategory",
        related_name="advertisements"
    )
    city = models.CharField(max_length=100, null=True, blank=True)
    province = models.CharField(max_length=100, null=True, blank=True)
    locality = models.CharField(max_length=255, null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    channel_image = models.URLField(blank=True, null=True)

    price_per_day = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Active Discount Fields
    apply_discount = models.BooleanField(default=False)
    discount_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_start_date = models.DateField(null=True, blank=True)
    discount_end_date = models.DateField(null=True, blank=True)

    # Deferred (Pending) Discount Fields
    pending_apply_discount = models.BooleanField(null=True, blank=True)
    pending_discount_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    pending_discount_start_date = models.DateField(null=True, blank=True)
    pending_discount_end_date = models.DateField(null=True, blank=True)
    pending_discount_effective_at = models.DateTimeField(
        null=True, blank=True,
        help_text="UTC time when pending changes will be promoted to live fields."
    )

    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=20, default="active")
    expiry_at = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=50, default="America/Toronto", null=True, blank=True)
    sub_business_categories = ArrayField(models.CharField(max_length=50), blank=True, default=list)
    phaseout_alert_sent = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Advertisement Channel"
        verbose_name_plural = "Advertisement Channels"

    def __str__(self):
        return self.channel_name

    def has_pending_discount_change(self):
        return self.pending_discount_effective_at is not None

    def is_discount_active_today(self):
        """Returns True only if the discount is currently active today in local timezone."""
        if not self.apply_discount:
            return False
            
        import zoneinfo
        from django.utils import timezone
        tz = zoneinfo.ZoneInfo(self.timezone or "America/Toronto")
        today = timezone.now().astimezone(tz).date()
        
        if self.discount_start_date and self.discount_start_date > today:
            return False
        if self.discount_end_date and self.discount_end_date < today:
            return False
            
        return True

    def get_discount_status_message(self):
        """Returns the dynamic discount message requested by the user"""
        if not self.apply_discount:
            return ""

        import zoneinfo
        from django.utils import timezone
        
        tz = zoneinfo.ZoneInfo("America/Toronto")
        now = timezone.now().astimezone(tz)
        today = now.date()
        
        # 1. Before starting
        if self.discount_start_date and self.discount_start_date > today:
            from datetime import timedelta
            if self.discount_start_date == today + timedelta(days=1):
                return f"Starts at 3 am Toronto"
            return ""
            
        # 3. Before ending (e.g. today is the end date)
        if self.discount_end_date and self.discount_end_date == today:
            return "Discount ending in 24 hrs"
            
        # 2. After starting / During active window
        try:
            if self.discount_price is not None:
                percent = int(self.discount_price)
                return f"{percent}% discount applied"
        except Exception:
            pass
            
        return "Discount applied"



class ChannelDiscountHistory(models.Model):
    """
    Records each period a channel had a particular discount setting.
    Created initially when a channel is created, and updated each time
    apply_pending_discount_changes() runs at midnight.
    Enables accurate day-by-day billing across mid-month discount changes.
    """
    channel = models.ForeignKey(
        AdvertisementChannel,
        on_delete=models.CASCADE,
        related_name="discount_history"
    )
    apply_discount = models.BooleanField()
    discount_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True)
    effective_from = models.DateField()          # date this entry became active
    effective_until = models.DateField(null=True, blank=True)  # null = still active

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Channel Discount History"
        verbose_name_plural = "Channel Discount Histories"
        ordering = ["-effective_from"]
        indexes = [
            models.Index(fields=["channel", "effective_from"]),
        ]

    def __str__(self):
        status = "with" if self.apply_discount else "without"
        until = self.effective_until or "present"
        return f"{self.channel.channel_name}: {status} discount ({self.effective_from} → {until})"

class ChannelPriceHistory(models.Model):
    channel = models.ForeignKey(
        AdvertisementChannel,
        on_delete=models.CASCADE,
        related_name="price_history"
    )
    price_per_day = models.DecimalField(max_digits=10, decimal_places=2)
    effective_from = models.DateField()
    effective_until = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Channel Price History"
        verbose_name_plural = "Channel Price Histories"
        ordering = ["-effective_from"]
        indexes = [
            models.Index(fields=["channel", "effective_from"]),
        ]

    def __str__(self):
        until = self.effective_until or "present"
        return f"{self.channel.channel_name}: ${self.price_per_day} ({self.effective_from} → {until})"

class Advertisement(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("resolved", "Resolved"),
        ("flagged", "Flagged"),
    ]
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="advertisements"
    )
    advertisment_name = models.CharField(max_length=255)
    advertisment_description = models.TextField(blank=True)
    advertisement_image = ArrayField(
        models.URLField(blank=True),
        blank=True,
        default=list,
    )

    is_ever_reported = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    expiry_at = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(
        max_length=50,
        default="America/Toronto",
        null=True, blank=True,
        help_text="IANA timezone string (e.g. 'America/Vancouver'). Used by scheduler to determine local expiry date."
    )
    advertisment_cost = models.DecimalField(max_digits=10, decimal_places=2)
    advertisement_status = models.CharField(max_length=20, default="active")
    is_ever_flagged = models.BooleanField(default=False)
    escalation = models.BooleanField(default=False)
    first_report_date = models.DateTimeField(null=True, blank=True)
    is_reported = models.BooleanField(default=False)
    admin_actions = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="action not taken",
    )
    # Stripe Billing Fields
    stripe_subscription_id = models.CharField(max_length=255, null=True, blank=True)
    stripe_customer_id = models.CharField(max_length=255, null=True, blank=True)
    last_billed_at = models.DateTimeField(null=True, blank=True)
    billing_status = models.CharField(max_length=50, default="pending")  # active, past_due, canceled, pending
    
    # Traceability for reused advertisements
    reused_from = models.ForeignKey(
        'self', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='reused_to',
        help_text="The original advertisement this ad was duplicated/reused from."
    )
    phaseout_alert_sent = models.BooleanField(default=False)

    # Monthly Billing Link
    # invoice field removed in favor of InvoiceLineItem (reverse relation: invoice_line_items)

    class Meta:
        verbose_name = "Advertisement"
        verbose_name_plural = "Advertisements"

    def __str__(self):
        return f"Ad {self.id} - {self.user.email} - {self.advertisement_status}"

    def get_billing_history(self):
        """Returns all invoice line items associated with this advertisement."""
        return self.invoice_line_items.select_related('invoice').order_by('-period_start')

    def get_total_billed(self):
        """Calculates total amount billed for this advertisement."""
        from django.db.models import Sum
        return self.invoice_line_items.aggregate(total=Sum('amount'))['total'] or 0

    def get_daily_rate(self, for_date=None):
        """
        Calculates the daily rate for this ad on a specific date.
        Caller must prefetch channel_assignments__advertisement_channel__discount_history
        and channel_assignments__advertisement_channel__price_history to avoid N+1 queries.
        """
        from decimal import Decimal
        from django.utils import timezone

        if for_date is None:
            for_date = timezone.now().date()

        daily_rate = Decimal('0.00')

        for assignment in self.channel_assignments.all():
            channel = assignment.advertisement_channel

            if channel.end_date and for_date > channel.end_date:
                continue

            # Resolve base price from history; fall back to current value for legacy channels
            price_records = sorted(
                [p for p in channel.price_history.all()
                 if p.effective_from <= for_date and (p.effective_until is None or p.effective_until >= for_date)],
                key=lambda x: x.effective_from, reverse=True
            )
            price = Decimal(str(price_records[0].price_per_day)) if price_records else Decimal(str(channel.price_per_day))

            # Resolve discount from history (all() reads from prefetch cache — no extra DB hit)
            all_discount_history = list(channel.discount_history.all())
            valid_discounts = sorted(
                [h for h in all_discount_history
                 if h.effective_from <= for_date and (h.effective_until is None or h.effective_until >= for_date)],
                key=lambda x: x.effective_from, reverse=True
            )

            if valid_discounts:
                apply_disc = valid_discounts[0].apply_discount
                disc_price = Decimal(str(valid_discounts[0].discount_price))
            elif all_discount_history:
                apply_disc = False
                disc_price = None
            else:
                # Legacy channel with no history rows
                apply_disc = channel.apply_discount
                disc_price = Decimal(str(channel.discount_price)) if channel.discount_price else None

            if apply_disc and disc_price:
                price = price - (price * disc_price) / Decimal('100.00')

            daily_rate += price

        return daily_rate
    def is_billed_for_period(self, start_date, end_date):
        """Checks if there is any billing record overlapping with the given period."""
        return self.invoice_line_items.filter(
            period_start__lte=end_date,
            period_end__gte=start_date
        ).exists()

    @property
    def active_flag(self):
        """Returns the current unresolved flag, if any."""
        return self.flags.filter(resolved=False).first()

    @property
    def is_flagged(self):
        return self.active_flag is not None


class Listings(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("resolved", "Resolved"),
        ("flagged", "Flagged"),
    ]
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="listings"
    )
    listing_name = models.CharField(max_length=255)
    listing_description = models.TextField(blank=True)
    listing_image = ArrayField(
        models.URLField(blank=True),
        blank=True,
        default=list,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_ever_reported = models.BooleanField(default=False)
    listing_status = models.CharField(max_length=20, default="active")
    is_ever_flagged = models.BooleanField(default=False)
    first_report_date = models.DateTimeField(null=True, blank=True)
    is_reported = models.BooleanField(default=False)
    escalation = models.BooleanField(default=False)
    admin_actions = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="action not taken"
    )

    class Meta:
        verbose_name = "Listing"
        verbose_name_plural = "Listings"

    def __str__(self):
        return f"{self.listing_name} - {self.user.email}"

    @property
    def is_flagged(self):
        return hasattr(self, "flag")


class AdvertisementReports(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("ignored", "Ignored"),
        ("resolved", "Resolved"),
    ]
    advertisement = models.ForeignKey(
        Advertisement,
        on_delete=models.CASCADE,
        related_name="reports"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="reportedAds"
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending"
    )
    report_id = models.CharField(max_length=20, unique=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True)

    class Meta:
        # prevent duplicate reports
        unique_together = ('advertisement', 'user')

    def __str__(self):
        return f"Report by {self.user.email} on {self.advertisement.advertisment_name}"


class ListingReports(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("ignored", "Ignored"),
        ("resolved", "Resolved"),
    ]
    listing = models.ForeignKey(
        Listings,
        on_delete=models.CASCADE,
        related_name="reports"
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="reportedListings"
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True)
    report_id = models.CharField(max_length=20, unique=True, null=True)

    class Meta:
        # prevent duplicate reports
        unique_together = ('listing', 'user')

    def __str__(self):
        return f"Report by {self.user.email} on {self.listing.listing_name}"


class AdvertisementFlag(models.Model):
    advertisement = models.ForeignKey(
        Advertisement,
        on_delete=models.CASCADE,
        related_name="flags"
    )
    flagged_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="flagged_ads"
    )
    title = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    images = ArrayField(models.URLField(blank=True), blank=True, default=list)
    flagged_at = models.DateTimeField(default=timezone.now)
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Advertisement Flag"
        verbose_name_plural = "Advertisement Flags"

    def __str__(self):
        return f"Flagged Ad: {self.advertisement.advertisment_name} by {self.flagged_by.email if self.flagged_by else 'Unknown'}"


class Suspension(models.Model):
    ACTION_CHOICES = [
        ("suspended", "Suspended"),
        ("reactivated", "Reactivated"),
    ]
    business = models.ForeignKey(
        BusinessProfile,
        on_delete=models.CASCADE,
        related_name="suspension"
    )
    start_date = models.DateTimeField(auto_now_add=True)
    action = models.CharField(
        max_length=20, choices=ACTION_CHOICES, default=None, null=True, blank=True)
    suspended_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="suspended_businesses")
    reactivated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="reactivated_businesses"
    )
    suspension_reason = models.CharField(max_length=255, blank=True, null=True)
    business_response = models.CharField(max_length=255, blank=True, null=True)
    end_date = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True, null=True)


class ListingFlag(models.Model):
    listing = models.OneToOneField(
        "Listings",
        on_delete=models.CASCADE,
        related_name="flag"
    )
    flagged_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="flagged_listings"
    )
    title = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    images = ArrayField(models.URLField(blank=True), blank=True, default=list)
    flagged_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Listing Flag"
        verbose_name_plural = "Listing Flags"

    def __str__(self):
        return f"Flagged Listing: {self.listing.listing_name} by {self.flagged_by.email if self.flagged_by else 'Unknown'}"


class AdvertisementChannelAssignment(models.Model):
    advertisement = models.ForeignKey(
        Advertisement,
        on_delete=models.CASCADE,
        related_name="channel_assignments"
    )
    advertisement_channel = models.ForeignKey(
        AdvertisementChannel,
        on_delete=models.CASCADE,
        related_name="advertisement_assignments"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Advertisement Channel Assignment"
        verbose_name_plural = "Advertisement Channel Assignments"


class ClientProfile(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name="client_profile")
    gender = models.CharField(max_length=10, choices=[
        ("male", "Male"),
        ("female", "Female"),
        ("other", "Other"),
    ], null=True, blank=True)
    profession = models.TextField(blank=True, null=True)
    date_of_birth = models.DateField(null=True, blank=True)
    interests = ArrayField(
        base_field=models.TextField(),
        blank=True,
        default=list,
        help_text="List of interests for the client"
    )

    # ✅ Updated ManyToMany with bridge table
    selected_channels = models.ManyToManyField(
        AdvertisementChannel,
        through="ClientChannelSelection",
        related_name="selected_by_clients"
    )

    created_at = models.DateTimeField(default=timezone.now, blank=True)
    updated_at = models.DateTimeField(default=timezone.now, blank=True)

    def __str__(self):
        return f"ClientProfile: {self.user.email}"


class ClientLocation(models.Model):
    client = models.ForeignKey(
        "ClientProfile",
        on_delete=models.CASCADE,
        related_name="saved_locations"
    )
    latitude = models.FloatField(help_text="Latitude of the location")
    longitude = models.FloatField(help_text="Longitude of the location")
    address = models.TextField()
    city = models.CharField(max_length=100)
    location_type = models.CharField(max_length=100)
    country = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=20, null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.address} ({self.city}, {self.country})"


class ClientChannelSelection(models.Model):
    client = models.ForeignKey("ClientProfile", on_delete=models.CASCADE)
    channel = models.ForeignKey(
        "AdvertisementChannel", on_delete=models.CASCADE)
    selected_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("client", "channel")  # Prevent duplicate selections
        verbose_name = "Client Channel Selection"


class LikedBusinessProfile(models.Model):
    profile = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE, related_name="liked_business_profiles")
    business_profile = models.ForeignKey(
        "BusinessProfile",
        on_delete=models.CASCADE,
        related_name="likes"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        # Prevent duplicate likes
        unique_together = ("profile", "business_profile")

    def __str__(self):
        return f"{self.profile.user.username} liked {self.business_profile.name}"


class LikedAdvertisement(models.Model):
    client_profile = models.ForeignKey(
        "ClientProfile",
        on_delete=models.CASCADE,
        related_name="liked_advertisements"
    )
    advertisement = models.ForeignKey(
        "Advertisement",
        on_delete=models.CASCADE,
        related_name="liked_by_clients"
    )
    liked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        # Prevent duplicate likes
        unique_together = ("client_profile", "advertisement")
        verbose_name = "Liked Advertisement"
        verbose_name_plural = "Liked Advertisements"

    def __str__(self):
        return f"{self.client_profile.user.email} liked {self.advertisement.advertisment_name}"


class LikedAdvertisementChannel(models.Model):
    client_profile = models.ForeignKey(
        "ClientProfile",
        on_delete=models.CASCADE,
        related_name="liked_advertisement_channels"
    )
    advertisement_channel = models.ForeignKey(
        "AdvertisementChannel",
        on_delete=models.CASCADE,
        related_name="liked_by_clients"
    )
    liked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        # Prevent duplicate likes
        unique_together = ("client_profile", "advertisement_channel")
        verbose_name = "Liked Advertisement Channel"
        verbose_name_plural = "Liked Advertisement Channels"

    def __str__(self):
        return f"{self.client_profile.user.email} liked {self.advertisement_channel.channel_name}"


class SupportTicket(models.Model):
    SUBJECT_CHOICES = [
        ("Account & Access", "Account & Access"),
        ("Account Login Issues", "Account Login Issues"),
        ("Password Reset Request", "Password Reset Request"),
        ("Account Suspension Inquiry", "Account Suspension Inquiry"),
        ("Two-Factor Authentication (2FA) Issues",
         "Two-Factor Authentication (2FA) Issues"),
        ("Account Reactivation Request", "Account Reactivation Request"),
        ("Business Profile Management", "Business Profile Management"),
        ("Update Business Information", "Update Business Information"),
        ("Business Location/Address Change", "Business Location/Address Change"),
        ("Business Hours Update", "Business Hours Update"),
        ("Category/Subcategory Assignment", "Category/Subcategory Assignment"),
        ("Business Profile Approval Status", "Business Profile Approval Status"),
        ("Advertisement Management", "Advertisement Management"),
        ("Advertisement Creation Issues", "Advertisement Creation Issues"),
        ("Advertisement Not Appearing", "Advertisement Not Appearing"),
        ("Advertisement Flagged/Rejected", "Advertisement Flagged/Rejected"),
        ("Edit/Delete Advertisement", "Edit/Delete Advertisement"),
        ("Advertisement Performance Concerns",
         "Advertisement Performance Concerns"),
        ("Advertising Channels", "Advertising Channels"),
        ("Channel Selection Issues", "Channel Selection Issues"),
        ("Channel Pricing Inquiry", "Channel Pricing Inquiry"),
        ("Channel Assignment Request", "Channel Assignment Request"),
        ("Temporary Channel Questions", "Temporary Channel Questions"),
        ("Billing & Payments", "Billing & Payments"),
        ("Payment Method Issues", "Payment Method Issues"),
        ("Billing Discrepancy", "Billing Discrepancy"),
        ("Invoice Request", "Invoice Request"),
        ("Refund Request", "Refund Request"),
        ("Prorated Charges Inquiry", "Prorated Charges Inquiry"),
        ("Payment Processing Failed", "Payment Processing Failed"),
        ("Free Listing", "Free Listing"),
        ("Free Listing Creation Issues", "Free Listing Creation Issues"),
        ("Free Listing Not Visible", "Free Listing Not Visible"),
        ("Free Listing Metrics Question", "Free Listing Metrics Question"),
        ("Analytics & Reporting", "Analytics & Reporting"),
        ("Dashboard Access Issues", "Dashboard Access Issues"),
        ("Metrics Not Displaying", "Metrics Not Displaying"),
        ("Report Generation Problem", "Report Generation Problem"),
        ("Performance Data Questions", "Performance Data Questions"),
        ("Technical Issues", "Technical Issues"),
        ("Platform Error/Bug Report", "Platform Error/Bug Report"),
        ("Map Integration Issues", "Map Integration Issues"),
        ("Image Upload Problems", "Image Upload Problems"),
        ("General Technical Support", "General Technical Support"),
        ("Policy & Compliance", "Policy & Compliance"),
        ("Content Guidelines Clarification", "Content Guidelines Clarification"),
        ("Terms of Service Question", "Terms of Service Question"),
        ("Platform Policy Inquiry", "Platform Policy Inquiry"),
        ("General", "General"),
        ("Feature Request", "Feature Request"),
        ("General Inquiry", "General Inquiry"),
    ]

    ticket_number = models.CharField(
        max_length=20, unique=True, editable=False)
    subject = models.CharField(max_length=255, choices=SUBJECT_CHOICES)
    full_name = models.CharField(max_length=255)
    designation = models.CharField(max_length=255)
    message = models.TextField()
    images = models.JSONField(default=list, blank=True)  # store image URLs
    business_uuid = models.UUIDField(null=True, blank=True)
    business_name = models.CharField(max_length=255, null=True, blank=True)
    business_email = models.EmailField(null=True, blank=True)
    business = models.ForeignKey(
        BusinessProfile,
        on_delete=models.CASCADE,
        related_name="support_emails"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.ticket_number} - {self.subject}"


class MonthlyInvoice(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("paid", "Paid"),
        ("grace", "Grace Period"),
        ("failed", "Failed"),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="invoices"
    )
    start_date = models.DateField()
    end_date = models.DateField()
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="pending"
    )

    grace_expires_at = models.DateTimeField(null=True, blank=True)

    stripe_payment_intent_id = models.CharField(max_length=255, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Monthly Invoice"
        verbose_name_plural = "Monthly Invoices"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['status', 'grace_expires_at']),
        ]

    def __str__(self):
        return f"Invoice {self.id} - {self.user.email} - {self.start_date.strftime('%Y-%m')} - {self.status}"

    def start_grace_period(self, days=3):
        """
        Set invoice to grace status with expiration date.
        
        Args:
            days (int): Number of days for grace period (default: 3)
        """
        from datetime import timedelta, datetime
        import zoneinfo
        
        self.status = 'grace'
        
        # Calculate local midnight in business timezone 3 days from now
        tz_name = "America/Toronto"
        try:
            tz_name = self.user.business_profile.timezone_id or "America/Toronto"
        except Exception:
            pass
            
        business_tz = zoneinfo.ZoneInfo(tz_name)
        future_date = (timezone.now().astimezone(business_tz) + timedelta(days=days)).date()
        
        # Combine with midnight and convert to UTC
        local_midnight = datetime.combine(future_date, datetime.min.time(), tzinfo=business_tz)
        self.grace_expires_at = local_midnight.astimezone(timezone.utc)
        
        self.save(update_fields=['status', 'grace_expires_at', 'updated_at'])

    def is_grace_expired(self):
        """
        Check if grace period has expired.
        
        Returns:
            bool: True if grace period has expired, False otherwise
        """
        return (
            self.status == 'grace' and 
            self.grace_expires_at and 
            timezone.now() >= self.grace_expires_at
        )

    def save(self, *args, **kwargs):
        # Immutability Check: Prevent modification of paid invoices
        if self.pk:
            original = MonthlyInvoice.objects.get(pk=self.pk)
            if original.status == 'paid' and self.status == 'paid':
                # Allow updating stripe_payment_intent_id if it was just set (transitioning to paid)
                # But if it's already paid, basic fields should not change.
                # Simplest check: if total_amount changed?
                if original.total_amount != self.total_amount:
                     raise ValueError("Cannot modify total_amount of a paid invoice.")
                # Can add more checks here
        super().save(*args, **kwargs)


class InvoiceLineItemType(models.TextChoices):
    ADVERTISEMENT = 'advertisement', 'Advertisement'
    ADJUSTMENT = 'adjustment', 'Adjustment'
    CREDIT = 'credit', 'Credit'


class InvoiceLineItem(models.Model):
    invoice = models.ForeignKey(
        MonthlyInvoice,
        on_delete=models.CASCADE,
        related_name='line_items'
    )
    # Generic relationship to support Advertisements (and potentially other things later)
    # For now, we can just use explicit ForeignKey to Advertisement as requested, 
    # but Implementation Plan suggested generic or specific. 
    # Plan said: "GenericForeignKey is flexible but complex. explicit ForeignKey is better for now."
    # Let's check Implementation Plan again.
    # Plan: "Fields: invoice (FK), advertisement (FK, nullable), description, amount, period_start, period_end"
    
    advertisement = models.ForeignKey(
        'Advertisement',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='invoice_line_items'
    )
    
    item_type = models.CharField(
        max_length=20,
        choices=InvoiceLineItemType.choices,
        default=InvoiceLineItemType.ADVERTISEMENT
    )
    
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    
    period_start = models.DateField()
    period_end = models.DateField()
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = "Invoice Line Item"
        verbose_name_plural = "Invoice Line Items"
        indexes = [
            models.Index(fields=['invoice', 'item_type']),
        ]

    def __str__(self):
        return f"{self.item_type}: {self.description} ({self.amount})"


class Notification(models.Model):
    NOTIFICATION_TYPES = [
        ("channel_phaseout", "Channel Phase-out Alert"),
        ("discount_start", "Discount Start"),
        ("discount_started", "Discount Started"),
        ("discount_ended", "Discount Ended"),
        ("discount_ending", "Discount Ending Soon"),
        ("discount_available", "Discount Available"),
        ("payment_success", "Payment Success"),
        ("payment_failed", "Payment Failed"),
        ("invoice_generated", "Invoice Generated"),
        ("ad_flagged_by_admin", "Ad Flagged by Admin"),
        ("ad_reported", "Ad Reported by User"),
        ("listing_reported", "Listing Reported by User"),
        ("grace_period_started", "Grace Period Started"),
        ("grace_period_expired", "Grace Period Expired"),
        ("account_suspended", "Account Suspended"),
        ("business_approved", "Business Account Approved"),
        ("business_approved_billing_reminder", "Business Approved - Billing Reminder"),
        ("ad_phased_out", "Advertisement Phased Out"),
    ]

    RECIPIENT_ROLE_CHOICES = [
        ("business", "Business"),
        ("user", "User"),
        ("admin", "Admin"),
    ]

    recipient = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="notifications"
    )
    notification_type = models.CharField(max_length=50, choices=NOTIFICATION_TYPES)
    title = models.CharField(max_length=255)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    recipient_role = models.CharField(max_length=10, choices=RECIPIENT_ROLE_CHOICES)

    # Optional references for navigation context
    advertisement_id = models.IntegerField(null=True, blank=True)
    channel_id = models.IntegerField(null=True, blank=True)
    invoice_id = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "is_read"]),
            models.Index(fields=["recipient", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.notification_type}: {self.title} → {self.recipient.email}"

