from typing import Optional, List
import calendar
from datetime import date, timedelta
from graphql import GraphQLError
import strawberry
from strawberry.types import Info
from django.db.models import Prefetch
from api.utils import attach_flag, build_advertisement_response, build_channel_ad_response, build_discount_history, build_listing_response
from api.stripe_services import get_user_payment_methods
from api.decorators import get_access_token_jti
from api.types import (ReportedItemsType, ReportedListingsType,
                       AdvertisementChannelReturnType, AdvertisementChannelType, AdvertisementReturnType, AdvertisementType, AdvertisementWithChannlesReturnType, BusinessHoursReturnType, BusinessHoursType, BusinessProfileDetailsType, BusinessProfileReturnType, BusinessProfileType, BusinessSubCategoryType, BusinessUserProfileType, ChannelAdvertisementReturnType, ChannelDetailsType, ChannelType, ChannelWithAdsType, ClientLocationType, ClientProfileType, ContactInfoType, ListingType, LocationType,
                       ProfileType, ReportedAdsType, ReportsResponseType, SuspensionType, UserReportsType, UserType, BusinessCategoryType, AdvertisementFlagType, PaymentMethodType,
                       BillingSummaryType, MonthlyInvoiceType, NotificationType,
                       ChannelDailySpendType, DayBillingType, SettingsSessionStatusType
                       )
from api.models import (
    Advertisement, AdvertisementChannel, AdvertisementReports, BusinessProfile, ClientProfile, LikedAdvertisement, ListingReports, Suspension, LikedBusinessProfile, Listings, User, Profile, BusinessCategory, AdvertisementFlag, AdvertisementChannelAssignment, MonthlyInvoice
)
from typing import Optional, List
from geopy.distance import geodesic

from api.decorators import jwt_required, require_api_secret, require_authentication, require_role, require_settings_verified
import logging
from api.decorators import jwt_required, require_api_secret, require_authentication, require_role, require_settings_verified
import logging
from django.db.models import Q
from django.utils import timezone

from django.db.models import Count, F
from django.db.models.functions import ACos, Cos, Radians, Sin
from django.db.models import F
from datetime import timedelta
from decimal import Decimal
from api.tasks import calculate_flagged_days

logger = logging.getLogger(__name__)


@strawberry.type
class Query:

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_settings_verified
    def billing_summary(self, info: Info) -> BillingSummaryType:
        user = info.context.request.user
        
        # 1. Calculate current month projected cost
        today = timezone.now().date()
        current_month_start = today.replace(day=1)
        _, last_day = calendar.monthrange(today.year, today.month)
        current_month_end = today.replace(day=last_day)
        
        # 'phaseout' add kiya gaya hai taake jo ads mid-month expire hue hain unka pichla bill count ho
        current_ads = Advertisement.objects.filter(
            user=user,
            billing_status__in=['pending', 'active', 'phaseout'], 
            start_date__lte=current_month_end
        ).exclude(
            end_date__lt=current_month_start
        ).prefetch_related(
            'channel_assignments__advertisement_channel__discount_history', 
            'flags'
        )
        
        current_cost = Decimal('0.00')
        
        for ad in current_ads:
            active_start = max(ad.start_date, current_month_start)
            current_ad_end = ad.end_date if ad.end_date else current_month_end
            active_end = min(current_ad_end, current_month_end)
            
            if active_end < active_start:
                continue
                
            # Un dino ki list nikal lein jinme ad flag tha
            flagged_days = calculate_flagged_days(ad, active_start, active_end)
            
            # Din-ba-din (day-by-day) loop chalayein
            current_date = active_start
            while current_date <= active_end:
                # Agar us din Ad par Flag nahi laga tha, sirf tabhi bill charge karein
                if current_date not in flagged_days:
                    # Us specifically day ka target rate uthayein
                    daily_rate = ad.get_daily_rate(current_date)
                    current_cost += Decimal(str(daily_rate))
                    
                current_date += timedelta(days=1)

        # 2. Fetch past invoices
        invoices = MonthlyInvoice.objects.filter(user=user).order_by('-created_at')
        
        invoice_types = [
            MonthlyInvoiceType(
                id=inv.id,
                start_date=inv.start_date,
                end_date=inv.end_date,
                total_amount=float(inv.total_amount),
                status=inv.status,
                stripe_payment_intent_id=inv.stripe_payment_intent_id,
                created_at=inv.created_at
            ) for inv in invoices
        ]

        return BillingSummaryType(
            current_month_cost=float(current_cost),
            billing_period_start=current_month_start,
            invoices=invoice_types
        )

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_settings_verified
    def daily_channel_billing(self, info: Info, month: str) -> List[DayBillingType]:
        """
        Kisi bhi month ka har din ka channel-wise billing breakdown return karta hai.
        Frontend billing page is data ko daily spend table mein show karta hai.
        """
        import zoneinfo
        from datetime import datetime as dt
        from api.tasks import calculate_flagged_days

        user = info.context.request.user

        # Request kiye gaye month ki start aur end dates nikalna
        year, month_num = map(int, month.split("-"))
        month_start = date(year, month_num, 1)
        _, last_day = calendar.monthrange(year, month_num)
        month_end = date(year, month_num, last_day)

        # Toronto 3 AM master clock — backend ke cron job ke saath match karta hai.
        # 3 AM Toronto = midnight Vancouver, yaani Canada ke tamam cities mein din guzar chuka hota hai.
        # Agar abhi Toronto mein 3 AM se pehle hai toh aaj ka din abhi finalize nahi hua,
        # isliye yesterday tak ka data dikhate hain.
        toronto_tz = zoneinfo.ZoneInfo("America/Toronto")
        now_toronto = dt.now(toronto_tz)
        today_toronto = now_toronto.date()
        if now_toronto.hour < 3:
            today_toronto -= timedelta(days=1)

        # Current month ke liye sirf finalized days tak data dikhao.
        # Past months ke liye poora month dikhao (already finalized hai).
        this_month_start = date.today().replace(day=1)
        effective_end = min(month_end, today_toronto) if month_start >= this_month_start else month_end

        # Agar month abhi shuru hi nahi hua (future month) toh kuch nahi hai
        if effective_end < month_start:
            return []

        # Us user ke woh ads fetch karo jo is month mein active thay ya hain.
        # prefetch_related se discount_history aur price_history ek hi query mein load ho jati hai —
        # warna day-by-day loop mein har channel ke liye alag DB hit hoti (N+1 problem).
        ads = Advertisement.objects.filter(
            user=user,
            billing_status__in=['pending', 'active', 'phaseout'],
            start_date__lte=effective_end
        ).exclude(
            end_date__lt=month_start
        ).prefetch_related(
            'channel_assignments__advertisement_channel__discount_history',
            'channel_assignments__advertisement_channel__price_history',
            'flags'
        )

        # Poore month ke flagged days ek baar calculate karo — har ad ke liye.
        # Warna har din × har ad ke liye alag call hoti (30 days × 3 ads = 90 calls).
        ad_flagged_days: dict = {
            ad.id: calculate_flagged_days(ad, month_start, effective_end)
            for ad in ads
        }

        result = []
        current_date = month_start

        # Har din ke liye ek DayBillingType entry banao
        while current_date <= effective_end:
            details = []

            for ad in ads:
                # Woh ads skip karo jo is din active nahi thay
                if ad.start_date > current_date:
                    continue
                if ad.end_date and ad.end_date < current_date:
                    continue

                # Pre-computed set mein sirf O(1) lookup — koi extra DB hit nahi
                is_flagged = current_date in ad_flagged_days[ad.id]

                for assignment in ad.channel_assignments.all():
                    channel = assignment.advertisement_channel

                    # Agar channel is din se pehle expire ho chuka hai toh skip karo
                    if channel.end_date and current_date > channel.end_date:
                        continue

                    # ChannelPriceHistory se us din ka sahi base price nikalo.
                    # Agar admin ne price change kiya ho toh purane dinon pe purana price apply hoga,
                    # naya price sirf naye din se lagega. Legacy channels (jinka koi history nahi)
                    # ke liye seedha channel.price_per_day use hoga.
                    price_records = sorted(
                        [p for p in channel.price_history.all()
                         if p.effective_from <= current_date and (p.effective_until is None or p.effective_until >= current_date)],
                        key=lambda x: x.effective_from, reverse=True
                    )
                    base_price = float(price_records[0].price_per_day) if price_records else float(channel.price_per_day)

                    # ChannelDiscountHistory se us din ka active discount nikalo.
                    # list() isliye ki .all() ek baar call karke prefetch cache se data aaye,
                    # warna valid_histories aur `elif not` dono alag queries fire karte.
                    all_discount_history = list(channel.discount_history.all())
                    valid_histories = sorted(
                        [h for h in all_discount_history
                         if h.effective_from <= current_date and (h.effective_until is None or h.effective_until >= current_date)],
                        key=lambda x: x.effective_from, reverse=True
                    )
                    discount_pct = 0.0
                    if valid_histories:
                        # Is din ka valid discount history entry mila
                        h = valid_histories[0]
                        if h.apply_discount and h.discount_price:
                            discount_pct = float(h.discount_price)
                    elif not all_discount_history:
                        # Legacy channel — koi history nahi, seedha channel fields dekho
                        if channel.apply_discount and channel.discount_price:
                            discount_pct = float(channel.discount_price)

                    # Final cost: flagged din pe zero, warna base price mein se discount ghataao
                    cost = base_price * (1 - discount_pct / 100) if not is_flagged else 0.0

                    # Ek channel pe ek hi ad ho sakta hai (enforced at creation),
                    # isliye har channel ka ek entry directly details mein jaata hai
                    details.append(ChannelDailySpendType(
                        channel_name=channel.channel_name,
                        price_per_day=base_price,
                        discount=discount_pct,
                        cost=round(cost, 2),
                        flagged=is_flagged,
                    ))

            # Sirf woh din result mein daalo jis din koi active channel tha
            if details:
                result.append(DayBillingType(
                    date=current_date,
                    channels=[d.channel_name for d in details],
                    total_cost=round(sum(d.cost for d in details), 2),
                    is_flagged=any(d.flagged for d in details),
                    details=details,
                ))
            current_date += timedelta(days=1)
        return result

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_business_billing_summary(self, info: Info, business_id: int) -> BillingSummaryType:
        # 1. Get the user associated with this business profile
        try:
            business_profile = BusinessProfile.objects.select_related('user').get(id=business_id)
            target_user = business_profile.user
        except BusinessProfile.DoesNotExist:
            raise GraphQLError("Business Profile not found")

        # 2. Calculate current month projected cost for THAT user
        today = timezone.now().date()
        current_month_start = today.replace(day=1)
        _, last_day = calendar.monthrange(today.year, today.month)
        current_month_end = today.replace(day=last_day)
        
        current_ads = Advertisement.objects.filter(
            user=target_user,
            billing_status__in=['pending', 'active'],
            start_date__lte=current_month_end
        ).exclude(
            end_date__lt=current_month_start
        ).prefetch_related('channel_assignments__advertisement_channel', 'flags')
        
        current_cost = Decimal('0.00')
        for ad in current_ads:
            active_start = max(ad.start_date, current_month_start)
            current_ad_end = ad.end_date if ad.end_date else current_month_end
            active_end = min(current_ad_end, current_month_end)
            
            if active_end < active_start:
                continue
            
            days_active = (active_end - active_start).days + 1
            flagged_days = calculate_flagged_days(ad, active_start, active_end)
            days_billable = max(0, days_active - len(flagged_days))
            
            if days_billable > 0:
                # 3. Derive Daily Rate from total cost for THAT user
                daily_rate = ad.get_daily_rate()
                if ad.advertisment_cost > 0 and ad.end_date:
                    total_days = (ad.end_date - ad.start_date).days + 1
                    if total_days > 0:
                        daily_rate = ad.advertisment_cost / Decimal(str(total_days))
                
                current_cost += daily_rate * days_billable

        # 3. Fetch past invoices for THAT user
        invoices = MonthlyInvoice.objects.filter(user=target_user).order_by('-created_at')
        
        invoice_types = [
            MonthlyInvoiceType(
                id=inv.id,
                start_date=inv.start_date,
                end_date=inv.end_date,
                total_amount=float(inv.total_amount),
                status=inv.status,
                stripe_payment_intent_id=inv.stripe_payment_intent_id,
                created_at=inv.created_at
            ) for inv in invoices
        ]

        return BillingSummaryType(
            current_month_cost=float(current_cost),
            billing_period_start=current_month_start,
            invoices=invoice_types
        )

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def settings_session_status(self, info: Info) -> SettingsSessionStatusType:
        """Whether the caller currently holds an active Settings OTP session. Returns no sensitive data."""
        from api.models import SettingsAccess
        access = SettingsAccess.objects.filter(user=info.context.request.user).first()
        if not access or not access.is_valid_for(get_access_token_jti(info)):
            return SettingsSessionStatusType(verified=False, expires_at=None, seconds_remaining=0)
        remaining = max(0, int((access.verified_until - timezone.now()).total_seconds()))
        return SettingsSessionStatusType(
            verified=True, expires_at=access.verified_until, seconds_remaining=remaining)

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def has_billing_method(self, info: Info) -> bool:
        """Whether the business has at least one card on file. Boolean only, so it is
        safe outside Settings (billing reminder banner)."""
        user = info.context.request.user
        return len(get_user_payment_methods(user) or []) > 0

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_payment_methods(self, info: Info) -> strawberry.scalars.JSON:
        user = info.context.request.user
        return get_user_payment_methods(user)

    @strawberry.field
    @require_api_secret
    @require_authentication
    def get_user(self, info: Info, user_id: int) -> UserType:
        # A caller may only read their own record. Checked before the lookup so
        # a foreign id gets the same answer whether or not that user exists.
        if int(user_id) != info.context.request.user.id:
            raise GraphQLError("You can only view your own account.")
        try:
            user = User.objects.get(id=user_id)

            clientProfile = None
            business_profile = None

            if hasattr(user, "client_profile") and user.role == "user":
                clientProfile = ClientProfileType(
                    id=user.client_profile.id,
                    gender=user.client_profile.gender,
                    date_of_birth=user.client_profile.date_of_birth.isoformat(
                    ) if user.client_profile.date_of_birth else None
                )
            business_profile_obj = getattr(
                user, "business_profile", None)
            if business_profile_obj and user.role == "business":

                suspension_obj = getattr(
                    business_profile_obj, "suspension", None)

                latest_suspension = None
                if suspension_obj and hasattr(suspension_obj, "all"):
                    suspension = suspension_obj.all().order_by("-start_date").first()

                    print("suspension_obj", suspension_obj)
                    # for suspension in suspension_obj:
                    #     # suspension = None
                    if suspension:
                        suspended_by = None
                        reactivated_by = None

                        # Check which field exists and populate accordingly
                        if hasattr(suspension, "suspended_by") and suspension.suspended_by:
                            suspended_by_user = suspension.suspended_by
                            suspended_by = UserType(
                                id=suspended_by_user.id,
                                first_name=suspended_by_user.first_name,
                                last_name=suspended_by_user.last_name,
                                email=suspended_by_user.email,
                                role=suspended_by_user.role,
                                is_active=suspended_by_user.is_active,
                            )

                        elif hasattr(suspension, "reactivated_by") and suspension.reactivated_by:
                            reactivated_by_user = suspension.reactivated_by
                            reactivated_by = UserType(
                                id=reactivated_by_user.id,
                                first_name=reactivated_by_user.first_name,
                                last_name=reactivated_by_user.last_name,
                                email=reactivated_by_user.email,
                                role=reactivated_by_user.role,
                                is_active=reactivated_by_user.is_active,
                            )
                        latest_suspension = SuspensionType(
                            id=suspension.id,
                            suspension_reason=suspension.suspension_reason,
                            note=suspension.note,
                            suspended_by=suspended_by,
                            reactivated_by=reactivated_by,
                            start_date=suspension.start_date,
                            end_date=suspension.end_date,
                            is_active=suspension.is_active,
                        )
                        #         suspensions.append(SuspensionType(
                        #             id=suspension.id,
                        #             suspension_reason=suspension.suspension_reason,
                        #             note=suspension.note,

                        #             suspended_by=suspended_by,
                        #             reactivated_by=reactivated_by,
                        #             start_date=suspension.start_date,
                        #             end_date=suspension.end_date
                        #         ))
                business_profile = BusinessUserProfileType(
                    business_name=user.business_profile.business_name,
                    description=user.business_profile.description,
                    logo_url=user.business_profile.logo_url,
                    address=user.business_profile.address,
                    city=user.business_profile.city,
                    state=user.business_profile.state,
                    zip_code=user.business_profile.zip_code,
                    latitude=user.business_profile.latitude,
                    longitude=user.business_profile.longitude,
                    phone=user.business_profile.phone,
                    website=user.business_profile.website,
                    payment_methods=user.business_profile.payment_methods or None,
                    subcategories=user.business_profile.subcategories or None,
                    category_id=user.business_profile.category.id if user.business_profile.category else None,
                    cover_image=user.business_profile.cover_image, is_suspended=user.business_profile.is_suspended,
                    timezoneId=user.business_profile.timezone_id,
                    suspension=latest_suspension,
                )

            return UserType(
                id=user.id,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
                role=user.role,
                clientProfile=clientProfile,
                business_profile=business_profile,
                is_active=user.is_active,
                is_verified=user.is_verified,
                is_admin_approved=user.is_admin_approved,
            )
        except User.DoesNotExist:
            raise Exception("User not found")

    @strawberry.field
    @require_api_secret
    @require_authentication
    def list_all_users(self, info: Info) -> list[UserType]:
        """
        Get a list of all users.
        """

        users = User.objects.all()
        print(users)
        return [
            UserType(
                id=user.id,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
                role=user.role,
                profile=getattr(user, "profile", None) or Profile(user=user),
                is_active=user.is_active
            )
            for user in users
        ]

    @strawberry.field
    @require_api_secret
    @require_authentication
    def business_category(self, info: Info, id: strawberry.ID) -> Optional[BusinessCategoryType]:
        try:
            return BusinessCategory.objects.get(pk=id)
        except BusinessCategory.DoesNotExist:
            return None

    @strawberry.field
    @require_api_secret
    @require_authentication
    def all_business_categories(self, info: Info) -> List[BusinessCategoryType]:
        return BusinessCategory.objects.all()

    @strawberry.field
    @require_authentication
    @require_role(["business"])
    def get_business_profile(self, info: Info) -> BusinessProfileReturnType:
        user = info.context["request"].user
        business_profile = user.business_profile

        return BusinessProfileReturnType(
            business_name=business_profile.business_name,
            business_uuid=str(business_profile.business_uuid) if business_profile.business_uuid else None,
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
            is_suspended=business_profile.is_suspended,
            timezoneId=business_profile.timezone_id,
            business_hours=[
                BusinessHoursReturnType(
                    day=bh.day,
                    opening_time=str(
                        bh.opening_time) if bh.opening_time else None,
                    closing_time=str(
                        bh.closing_time) if bh.closing_time else None,
                    is_closed=bh.is_closed,
                    is_overnight=bh.is_overnight or False,
                    is_24hours=bh.is_24hours or False
                )
                for bh in business_profile.business_hours.all()
            ]
        )

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def get_advertisement_channels(
        self,
        info: Info,
        status: Optional[str] = None,
        city: Optional[str] = None,
        province: Optional[str] = None,
        categories: Optional[List[str]] = None
    ) -> list[AdvertisementType]:
        filters = {}

        if status in ["active", "disabled", "phaseout"]:
            filters["status"] = status

        user = info.context["request"].user
        if user.role == "business":
            business_profile = user.business_profile
            # If the channel is restricted to a province, it must match the business's province
            # If the channel is restricted to a city, it must match the business's city
            # Global channels (province=None, city=None) are always visible
            ads = AdvertisementChannel.objects.filter(**filters).filter(
                Q(province__isnull=True) | Q(province="") | Q(province__iexact=business_profile.state)
            ).order_by("-created_at")
        else:
            if city:
                filters["city__iexact"] = city

            if province:
                filters["province__iexact"] = province

            ads = AdvertisementChannel.objects.filter(**filters).order_by("-created_at")

        if categories:
            ads = ads.filter(
                advertisement_category__category__name__in=categories).distinct().order_by("-created_at")

        return [
            AdvertisementType(
                id=ad.id,
                channel_name=ad.channel_name,
                business_categories=[
                    ac.category.name for ac in ad.advertisement_category.select_related("category").all()
                ],
                city=ad.city,
                province=ad.province,
                locality=ad.locality,
                latitude=ad.latitude,
                longitude=ad.longitude,
                sub_business_categories=ad.sub_business_categories,
                price_per_day=float(ad.price_per_day),
                apply_discount=ad.apply_discount,
                discount_status_message=ad.get_discount_status_message(),
                start_date=str(ad.start_date),
                end_date=str(ad.end_date),
                status=ad.status,
                discount_price=ad.discount_price,
                discount_start_date=ad.discount_start_date,
                discount_end_date=ad.discount_end_date,
                channel_image=ad.channel_image,
                pending_apply_discount=ad.pending_apply_discount,
                pending_discount_price=float(ad.pending_discount_price) if ad.pending_discount_price is not None else None,
                pending_discount_start_date=ad.pending_discount_start_date,
                pending_discount_end_date=ad.pending_discount_end_date,
                discount_effective_from=ad.pending_discount_effective_at,
                discount_history=build_discount_history(ad),
            )
            for ad in ads
        ]

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def get_advertisement_channel(self, info: Info, id: int) -> AdvertisementType:
        user = info.context["request"].user
        ad = AdvertisementChannel.objects.get(id=id)

        if user.role == "business":
            business_profile = user.business_profile
            # Basic validation: If channel is restricted, business must be in that location
            if ad.province and ad.province.lower() != business_profile.state.lower():
                raise GraphQLError("You do not have permission to view this channel (Province mismatch)")

        from api.types import ChannelDiscountHistoryType
        return AdvertisementType(
            id=ad.id,
            channel_name=ad.channel_name,
            business_categories=[
                ac.category.name for ac in ad.advertisement_category.select_related("category").all()
            ],
            city=ad.city,
            province=ad.province,
            locality=ad.locality,
            latitude=ad.latitude,
            longitude=ad.longitude,
            sub_business_categories=ad.sub_business_categories,
            price_per_day=float(ad.price_per_day),
            apply_discount=ad.apply_discount,
            discount_status_message=ad.get_discount_status_message(),
            start_date=str(ad.start_date),
            end_date=str(ad.end_date),
            status=ad.status,
            discount_price=ad.discount_price,
            discount_start_date=ad.discount_start_date,
            discount_end_date=ad.discount_end_date,
            channel_image=ad.channel_image,
            pending_apply_discount=ad.pending_apply_discount,
            pending_discount_price=float(ad.pending_discount_price) if ad.pending_discount_price is not None else None,
            pending_discount_start_date=ad.pending_discount_start_date,
            pending_discount_end_date=ad.pending_discount_end_date,
            discount_effective_from=ad.pending_discount_effective_at,
            discount_history=[
                ChannelDiscountHistoryType(
                    id=h.id,
                    apply_discount=h.apply_discount,
                    discount_price=float(h.discount_price) if h.discount_price is not None else None,
                    effective_from=h.effective_from,
                    effective_until=h.effective_until,
                )
                for h in ad.discount_history.order_by("effective_from")
            ],
        )

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def get_users_list(
        self,
        info: Info,
        role: str,
        is_admin_approved: Optional[bool] = None, is_active: Optional[bool] = False
    ) -> list[UserType]:
        try:
            users = User.objects.all()
            if is_admin_approved is True:
                users = users.filter(is_admin_approved=True)
            elif is_admin_approved is False:
                users = users.filter(is_admin_approved=False)
            elif is_admin_approved is None:
                users = users.filter(is_admin_approved__isnull=True)

            if is_active is True:
                users = users.filter(is_active=True)
            if role != "all":
                users = users.filter(role=role)

            result = []
            for user in users:
                # Get client profile safely
                client_profile_obj = getattr(user, "client_profile", None)
                clientProfile = None
                if client_profile_obj and user.role == "user":
                    clientProfile = ClientProfileType(
                        id=client_profile_obj.id,
                        gender=client_profile_obj.gender,
                        date_of_birth=client_profile_obj.date_of_birth.isoformat(
                        ) if client_profile_obj.date_of_birth else None,
                        # Include other fields if needed
                    )

                # Get business profile safely
                business_profile_obj = getattr(user, "business_profile", None)
                business_profile = None
                if business_profile_obj and user.role == "business":
                    suspension_obj = getattr(
                        business_profile_obj, "suspension", None)

                    latest_suspension = None
                    if suspension_obj and hasattr(suspension_obj, "all"):
                        suspension = suspension_obj.all().order_by("-start_date").first()

                        print("suspension_obj", suspension_obj)
                        # for suspension in suspension_obj:
                        #     # suspension = None
                        if suspension:
                            suspended_by = None
                            reactivated_by = None

                            # Check which field exists and populate accordingly
                            if hasattr(suspension, "suspended_by") and suspension.suspended_by:
                                suspended_by_user = suspension.suspended_by
                                suspended_by = UserType(
                                    id=suspended_by_user.id,
                                    first_name=suspended_by_user.first_name,
                                    last_name=suspended_by_user.last_name,
                                    email=suspended_by_user.email,
                                    role=suspended_by_user.role,
                                    is_active=suspended_by_user.is_active,
                                )

                            elif hasattr(suspension, "reactivated_by") and suspension.reactivated_by:
                                reactivated_by_user = suspension.reactivated_by
                                reactivated_by = UserType(
                                    id=reactivated_by_user.id,
                                    first_name=reactivated_by_user.first_name,
                                    last_name=reactivated_by_user.last_name,
                                    email=reactivated_by_user.email,
                                    role=reactivated_by_user.role,
                                    is_active=reactivated_by_user.is_active,
                                )
                            latest_suspension = SuspensionType(
                                id=suspension.id,
                                suspension_reason=suspension.suspension_reason,
                                note=suspension.note,
                                suspended_by=suspended_by,
                                reactivated_by=reactivated_by,
                                start_date=suspension.start_date,
                                end_date=suspension.end_date,
                                is_active=suspension.is_active,
                            )
                    # suspensions = []
                    # print("suspension_obj", suspension_obj)
                    # for suspension in suspension_obj.all():

                    #     if suspension:
                    #         # Default None
                    #         suspended_by = None
                    #         reactivated_by = None

                    #         # Check which field exists and populate accordingly
                    #         if hasattr(suspension, "suspended_by") and suspension.suspended_by:
                    #             suspended_by_user = suspension.suspended_by
                    #             suspended_by = UserType(
                    #                 id=suspended_by_user.id,
                    #                 first_name=suspended_by_user.first_name,
                    #                 last_name=suspended_by_user.last_name,
                    #                 email=suspended_by_user.email,
                    #                 role=suspended_by_user.role,
                    #                 is_active=suspended_by_user.is_active,
                    #             )

                    #         elif hasattr(suspension, "reactivated_by") and suspension.reactivated_by:
                    #             reactivated_by_user = suspension.reactivated_by
                    #             reactivated_by = UserType(
                    #                 id=reactivated_by_user.id,
                    #                 first_name=reactivated_by_user.first_name,
                    #                 last_name=reactivated_by_user.last_name,
                    #                 email=reactivated_by_user.email,
                    #                 role=reactivated_by_user.role,
                    #                 is_active=reactivated_by_user.is_active,
                    #             )

                    #     suspensions.append(SuspensionType(
                    #         id=suspension.id,
                    #         suspension_reason=suspension.suspension_reason,
                    #         note=suspension.note,
                    #         suspended_by=suspended_by,
                    #         reactivated_by=reactivated_by,
                    #         start_date=suspension.start_date,
                    #         end_date=suspension.end_date
                    #     ))
                    business_hours = []
                    business_hours_obj = getattr(
                        business_profile_obj, "business_hours", None)
                    print(hasattr(business_profile_obj, "business_hours"))
                    if hasattr(business_hours_obj, "all"):
                        for hour in business_hours_obj.all():
                            print("hour", hour.day, hour.opening_time)
                            business_hours.append(
                                BusinessHoursType(
                                    opening_time=hour.opening_time,
                                    closing_time=hour.closing_time,
                                    day="mon",
                                    is_closed=hour.is_closed,is_overnight=hour.is_overnight,is_24hours=hour.is_24hours
                                )
                            )
                            print("business_hours", business_hours)

                    business_profile = BusinessUserProfileType(
                        business_name=business_profile_obj.business_name,
                        description=business_profile_obj.description,
                        logo_url=business_profile_obj.logo_url,
                        cover_image=business_profile_obj.cover_image,
                        address=business_profile_obj.address,
                        city=business_profile_obj.city,
                        state=business_profile_obj.state,
                        zip_code=business_profile_obj.zip_code,
                        latitude=business_profile_obj.latitude,
                        longitude=business_profile_obj.longitude,
                        phone=business_profile_obj.phone,
                        website=business_profile_obj.website,
                        payment_methods=business_profile_obj.payment_methods or None,
                        subcategories=business_profile_obj.subcategories or None,
                        category_id=business_profile_obj.category.id if business_profile_obj.category else None,
                        suspension=latest_suspension,
                        hours=business_hours,
                        timezoneId=business_profile_obj.timezone_id,
                        is_suspended=business_profile_obj.is_suspended


                    )

                result.append(
                    UserType(
                        id=user.id,
                        email=user.email,
                        first_name=user.first_name,
                        last_name=user.last_name,
                        role=user.role,
                        created_at=user.created_at,
                        updated_at=user.updated_at,
                        is_active=user.is_active,
                        is_verified=user.is_verified,
                        clientProfile=clientProfile,
                        business_profile=business_profile
                    )
                )

            return result

        except Exception as e:
            raise GraphQLError(f"Error retrieving users: {str(e)}")

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def all_advertisements(
        self,
        info: Info,
        status: Optional[str] = None,
        # Accept list of channel names
        channel_names: Optional[List[str]] = None
    ) -> List[AdvertisementReturnType]:
        user = info.context["request"].user

        queryset = Advertisement.objects.prefetch_related("channel_assignments__advertisement_channel", "flags").select_related(
            "user__business_profile").filter(user=user)

        if status:
            queryset = queryset.filter(advertisement_status=status)

        if channel_names:
            queryset = queryset.filter(
                channel_assignments__advertisement_channel__channel_name__in=channel_names
            ).distinct()

        ads = queryset.all()
        return [build_advertisement_response(ad) for ad in ads]

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def get_reusable_advertisements(
        self,
        info: Info,
    ) -> List[AdvertisementReturnType]:
        """
        Returns advertisements owned by the current business user that are
        eligible to be reused:
          1. advertisement_status is NOT 'active'  (Status Lock)
          2. Have never been used as a source (reused_to count == 0)  (One-Time Limit)
        """
        from django.db.models import Count

        user = info.context["request"].user
        if user.role != "business":
            raise GraphQLError("Only business users can view reusable advertisements.")

        queryset = (
            Advertisement.objects
            .filter(user=user)
            .exclude(advertisement_status="active")
            .annotate(reuse_count=Count("reused_to"))
            .filter(reuse_count=0)
            .prefetch_related("channel_assignments__advertisement_channel", "flags")
            .select_related("user__business_profile")
            .order_by("-created_at")
        )

        return [build_advertisement_response(ad) for ad in queryset]

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def all_listings(
        self,
        info: Info,
        status: Optional[str] = None,
        city: Optional[str] = None,
        province: Optional[str] = None,
        categories: Optional[List[str]] = None,
    ) -> List[ListingType]:
        try:
            user = info.context["request"].user

            # Base queryset with related user + business_profile for optimization
            queryset = Listings.objects.select_related(
                "user__business_profile")

            # Restrict to only their listings if user is business
            if user.role == "business":
                queryset = queryset.filter(user=user)
            elif user.role != "admin":
                raise GraphQLError(
                    "Only users with role 'business' or 'admin' can view listings")

            # Apply filters
            if status:
                queryset = queryset.filter(listing_status=status)

            if city:
                queryset = queryset.filter(
                    user__business_profile__city__iexact=city)

            if province:
                queryset = queryset.filter(
                    user__business_profile__state__iexact=province)

            if categories and len(categories) > 0:
                queryset = queryset.filter(
                    user__business_profile__category__name__in=categories)

            listings = queryset.all()

            if not listings:
                # Optional: return empty list gracefully instead of error
                return []

            # Transform queryset into GraphQL return type
            return [build_listing_response(listing) for listing in listings]

        except Listings.DoesNotExist:
            raise GraphQLError("No listings found.")
        except Exception as e:
            raise GraphQLError(f"Error fetching listings: {str(e)}")

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def get_listing(self, info: Info, id: int) -> ListingType:
        listing = (
            Listings.objects.select_related("user", "flag")
            .filter(id=id)
            .first()
        )
        if not listing:
            raise Exception("Listing not found")

        return build_listing_response(listing)

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def get_advertisement(self, info: Info, id: int) -> AdvertisementReturnType:
        ad = Advertisement.objects.prefetch_related(
            "channel_assignments__advertisement_channel", "flags"
        ).select_related(
            "user__business_profile"
        ).filter(id=id).first()
        return build_advertisement_response(ad)

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def get_channel_with_advertisements_v1(
        self,
        info: Info,
        channel_id: int
    ) -> Optional[ChannelWithAdsType]:
        try:
            channel = AdvertisementChannel.objects.prefetch_related(
                Prefetch(
                    'advertisement_assignments',
                    queryset=AdvertisementChannelAssignment.objects.filter(
                        advertisement__advertisement_status="active"
                    ).select_related("advertisement").prefetch_related("advertisement__flags"),
                    to_attr='active_assignments'
                ),
                'business_categories'
            ).get(id=channel_id)

            ads = [
                build_channel_ad_response(assignment.advertisement)
                for assignment in channel.active_assignments
            ]

            # Add this: Fetch selected business categories
            business_categories = [
                BusinessCategoryType(
                    id=category.id,
                    name=category.name,
                    subcategories=[
                        BusinessSubCategoryType(id=sub.id, name=sub.name)
                        for sub in category.subcategories.all()
                    ],
                    created_at=category.created_at

                )
                for category in channel.business_categories.all()
            ]

            return ChannelWithAdsType(
                id=channel.id,
                channel_name=channel.channel_name,
                city=channel.city,
                province=channel.province,
                locality=channel.locality,
                latitude=channel.latitude,
                longitude=channel.longitude,
                price_per_day=channel.price_per_day,
                apply_discount=channel.is_discount_active_today(),
                discount_status_message=channel.get_discount_status_message(),
                discount_price=channel.discount_price,
                start_date=channel.start_date,
                end_date=channel.end_date,
                status=channel.status,
                channel_image=channel.channel_image,
                sub_business_categories=channel.sub_business_categories,
                advertisements=ads,
                business_categories=business_categories,
                discount_history=build_discount_history(channel),
            )

        except AdvertisementChannel.DoesNotExist:
            return None

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def get_channel_with_advertisements(
        self,
        info: Info,
        channel_id: int
    ) -> Optional[ChannelWithAdsType]:
        request = info.context.request
        user = request.user

        try:
            # Prefetch and filter advertisements
            assignment_qs = AdvertisementChannelAssignment.objects.filter(
                advertisement_channel_id=channel_id
            ).select_related('advertisement__user')

            channel = AdvertisementChannel.objects.prefetch_related(
                Prefetch(
                    'advertisement_assignments',
                    queryset=assignment_qs,
                    to_attr='filtered_assignments'
                ),
                'business_categories'
            ).get(id=channel_id)

            # Get relevant advertisement assignments
            if user.is_admin():
                filtered_assignments = channel.filtered_assignments
            elif user.is_business():
                filtered_assignments = [
                    a for a in channel.filtered_assignments
                    if a.advertisement.user_id == user.id
                ]
            else:
                filtered_assignments = [
                    a for a in channel.filtered_assignments
                    if a.advertisement.advertisement_status == "active"
                ]

            ads = [
                build_channel_ad_response(a.advertisement)
                for a in filtered_assignments
            ]

            # logger.info(f"All returned ads: {ads}")
            business_categories = [
                BusinessCategoryType(
                    id=category.id,
                    name=category.name,
                    subcategories=[
                        BusinessSubCategoryType(id=sub.id, name=sub.name)
                        for sub in category.subcategories.all()
                    ],
                    created_at=category.created_at

                )
                for category in channel.business_categories.all()
            ]

            # Get BusinessProfile from first advertisement's user
            business_profile_data = None
            if filtered_assignments:
                ad_user = filtered_assignments[0].advertisement.user
                if hasattr(ad_user, 'business_profile'):
                    bp = ad_user.business_profile
                    business_profile_data = BusinessProfileReturnType(
                        business_name=bp.business_name,
                        business_uuid=str(bp.business_uuid) if bp.business_uuid else None,
                        description=bp.description,
                        logo_url=bp.logo_url,
                        cover_image=bp.cover_image,
                        address=bp.address,
                        address2=bp.address2,
                        city=bp.city,
                        state=bp.state,
                        zip_code=bp.zip_code,
                        latitude=bp.latitude,
                        longitude=bp.longitude,
                        phone=bp.phone,
                        website=bp.website,
                        payment_methods=bp.payment_methods or [],
                        created_at=bp.created_at
                    )

            return ChannelWithAdsType(
                id=channel.id,
                channel_name=channel.channel_name,
                city=channel.city,
                province=channel.province,
                locality=channel.locality,
                latitude=channel.latitude,
                longitude=channel.longitude,
                price_per_day=channel.price_per_day,
                apply_discount=channel.is_discount_active_today(),
                discount_status_message=channel.get_discount_status_message(),
                discount_price=channel.discount_price,
                start_date=channel.start_date,
                end_date=channel.end_date,
                status=channel.status,
                sub_business_categories=channel.sub_business_categories,
                advertisements=ads,
                business_categories=business_categories,
                business_profile=business_profile_data,
                discount_history=build_discount_history(channel),
                channel_image=channel.channel_image
            )

        except AdvertisementChannel.DoesNotExist:
            return None

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    def flagged_advertisements(self, info) -> list[AdvertisementType]:
        return Advertisement.objects.filter(flags__isnull=False).distinct()

    @require_authentication
    @jwt_required
    @require_api_secret
    @strawberry.field
    def all_flags(self, info) -> list[AdvertisementFlagType]:
        return AdvertisementFlag.objects.select_related("advertisement", "flagged_by").all()

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_saved_locations(self, info: Info) -> list[ClientLocationType]:
        from api.models import ClientLocation

        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, 'client_profile'):
                raise GraphQLError("User does not have a client profile.")

            client_profile = user.client_profile

            locations = ClientLocation.objects.filter(client=client_profile)

            return [
                ClientLocationType(
                    id=loc.id,
                    latitude=loc.latitude,
                    longitude=loc.longitude,
                    address=loc.address,
                    city=loc.city,
                    country=loc.country,
                    postal_code=loc.postal_code,
                    location_type=loc.location_type,
                    created_at=loc.created_at,
                    updated_at=loc.updated_at
                )
                for loc in locations
            ]

        except Exception as e:
            raise GraphQLError(f"Error retrieving saved locations: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_client_profile(self, info: Info) -> ClientProfileType:
        try:
            user = info.context.request.user

            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, 'client_profile'):
                raise GraphQLError("User does not have a client profile.")

            client_profile = user.client_profile

            selected_channels = AdvertisementChannel.objects.filter(
                selected_by_clients=client_profile
            ).prefetch_related("business_categories")
            return ClientProfileType(
                id=client_profile.id,
                gender=client_profile.gender,
                profession=client_profile.profession,
                interests=client_profile.interests or [],
                created_at=client_profile.created_at,
                updated_at=client_profile.updated_at,
                selected_channels=[
                    ChannelType(
                        id=ch.id,
                        channel_name=ch.channel_name,
                        city=ch.city,
                        province=ch.province,
                        locality=ch.locality,
                        latitude=ch.latitude,
                        longitude=ch.longitude,
                        price_per_day=ch.price_per_day,
                        apply_discount=ch.is_discount_active_today(),
                        discount_status_message=ch.get_discount_status_message(),
                        discount_price=ch.discount_price,
                        start_date=str(ch.start_date),
                        end_date=str(ch.end_date) if ch.end_date else "",
                        status=ch.status,
                    )
                    for ch in selected_channels
                ]
            )
        except Exception as e:
            raise GraphQLError(f"Error fetching client profile: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_nearby_channels(
        self,
        info: Info,
        latitude: float,
        longitude: float,
        radius_in_km: float,
        category_ids: Optional[List[int]] = None,
        # "newest", "alphabetical", or "ad_count"
        sort_by: Optional[str] = "newest",
    ) -> List[AdvertisementChannelReturnType]:

        # Channels have no fixed location (province-wide or country-wide only).
        # Visibility is determined by whether any business with an active ad in the
        # channel is within the user's radius.
        active_ad_q = (
            Q(advertisement_assignments__advertisement__advertisement_status="active")
            & ~Q(advertisement_assignments__advertisement__flags__resolved=False)
            & Q(advertisement_assignments__advertisement__user__business_profile__is_suspended=False)
            & Q(advertisement_assignments__advertisement__user__business_profile__is_deactivated=False)
            & Q(advertisement_assignments__advertisement__user__is_active=True)
        )

        channels = AdvertisementChannel.objects.filter(status="active").prefetch_related(
            "advertisement_assignments__advertisement__user__business_profile",
            "business_categories"
        ).annotate(ad_count=Count("advertisement_assignments", filter=active_ad_q))

        # Apply category filtering
        if category_ids:
            channels = channels.filter(
                business_categories__id__in=category_ids).distinct()

        # Include a channel if at least one of its active-ad businesses is within the radius
        nearby_channels = []
        for channel in channels:
            for assignment in channel.advertisement_assignments.all():
                ad = assignment.advertisement
                if ad.advertisement_status != "active":
                    continue
                bp = getattr(ad.user, "business_profile", None)
                if bp is None or bp.latitude is None or bp.longitude is None:
                    continue
                if bp.is_suspended or bp.is_deactivated or not ad.user.is_active:
                    continue
                if geodesic((latitude, longitude), (bp.latitude, bp.longitude)).km <= radius_in_km:
                    nearby_channels.append(channel)
                    break  # one qualifying business is enough

        # Sort based on user input
        if sort_by == "alphabetical":
            nearby_channels.sort(key=lambda x: x.channel_name.lower())
        elif sort_by == "ad_count":
            nearby_channels.sort(key=lambda x: x.ad_count, reverse=True)
        else:  # default is "newest"
            nearby_channels.sort(key=lambda x: x.created_at, reverse=True)

        return nearby_channels

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_nearby_businesses(
        self,
        info: Info,
        latitude: float,
        longitude: float,
        radius_in_km: float,
        category_ids: Optional[List[int]] = None,
        # "newest", "alphabetical", or "ad_count"
        sort_by: Optional[str] = "newest"
    ) -> List[BusinessProfileType]:

        # Step 1: Start base query
        businesses = BusinessProfile.objects.filter(
            is_suspended=False,
            is_deactivated=False,
            user__is_active=True
        ).select_related("category", "user").prefetch_related("business_hours", "user__listings")

        # Step 2: Filter by category if provided
        if category_ids:
            businesses = businesses.filter(category_id__in=category_ids)

        # Step 3: Apply optional sorting
        if sort_by == "alphabetical":
            businesses = businesses.order_by("business_name")
        elif sort_by == "ad_count":
            active_ad_q = (
                Q(user__advertisements__advertisement_status="active")
                & ~Q(user__advertisements__flags__resolved=False)
            )
            businesses = businesses.annotate(
                ad_count=Count("user__advertisements", filter=active_ad_q)
            ).order_by("-ad_count")
        else:
            businesses = businesses.order_by("-created_at")  # Default = newest first

        # Step 4: Filter by distance and build result
        result = []
        for business in businesses:
            if business.latitude is not None and business.longitude is not None:
                distance = geodesic((latitude, longitude),
                                    (business.latitude, business.longitude)).km
                if distance <= radius_in_km:
                    from api.types import ListingType as ListingReturnType
                    listings = [
                        ListingReturnType(
                            id=l.id,
                            listing_name=l.listing_name,
                            listing_description=l.listing_description,
                            listing_image=l.listing_image or [],
                            listing_status=l.listing_status,
                            is_ever_flagged=l.is_ever_flagged,
                            is_reported=l.is_reported,
                            created_at=l.created_at,
                            updated_at=l.updated_at,
                        )
                        for l in business.user.listings.all()
                        if l.listing_status == "active"
                    ]
                    result.append(BusinessProfileType.from_instance(business, listings=listings))

        return result

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_business_profile_by_id(
        self,
        info: Info,
        business_Id: int,
        search: Optional[List[str]] = None
    ) -> Optional[BusinessProfileDetailsType]:
        try:
            business = (
                BusinessProfile.objects
                .select_related("user", "category")
                .prefetch_related(
                    "business_hours",
                    "user__advertisements",
                    "user__listings"   # 👈 prefetch listings for this business
                )
                .get(id=business_Id)
            )

            user = business.user

            # Filter advertisements: only active and not flagged
            advertisements = user.advertisements.filter(
                advertisement_status="active"
            ).prefetch_related('flags')

            if search:
                query = Q()
                for term in search:
                    query |= Q(advertisment_name__icontains=term)
                advertisements = advertisements.filter(query)

            advertisements = [ad for ad in advertisements if not ad.is_flagged]

            # Fetch listings
            listings = user.listings.all()

            return BusinessProfileDetailsType(
                id=business.id,
                business_name=business.business_name,
                description=business.description,
                logo_url=business.logo_url,
                cover_image=business.cover_image,
                is_suspended=business.is_suspended,
                hours=[
                    BusinessHoursType(
                        day=hour.day,
                        opening_time=hour.opening_time.isoformat() if hour.opening_time else None,
                        closing_time=hour.closing_time.isoformat() if hour.closing_time else None,
                        is_closed=hour.is_closed,is_overnight=hour.is_overnight,is_24hours=hour.is_24hours
                    )
                    for hour in business.business_hours.all()
                ],
                location=LocationType(
                    address=business.address,
                    city=business.city,
                    state=business.state,
                    zip_code=business.zip_code,
                    latitude=business.latitude,
                    longitude=business.longitude,
                ),
                contact=ContactInfoType(
                    phone=business.phone,
                    website=business.website
                ),
                payment_methods=business.payment_methods or [],
                category=business.category.name if business.category else None,
                subcategories=business.subcategories or [],
                user=UserType(
                    email=user.email,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    role=user.role,
                    id=user.id,
                    created_at=user.created_at,
                    updated_at=user.updated_at,
                    is_active=user.is_active,
                    is_verified=user.is_verified,
                ),
                advertisements=[
                    ChannelAdvertisementReturnType(
                        id=ad.id,
                        advertisment_name=ad.advertisment_name,
                        advertisment_description=ad.advertisment_description,
                        advertisement_image=ad.advertisement_image or [],
                        start_date=ad.start_date,
                        end_date=ad.end_date,
                        advertisment_cost=ad.advertisment_cost,
                        advertisement_status=ad.advertisement_status,
                        is_flagged=ad.is_flagged,
                        is_ever_flagged=ad.is_ever_flagged,
                        is_reported=ad.is_reported

                    )
                    for ad in advertisements
                ],
                listings=[
                    ListingType(
                        id=listing.id,
                        listing_name=listing.listing_name,
                        listing_description=listing.listing_description,
                        listing_image=listing.listing_image or [],
                        listing_status=listing.listing_status,
                        is_ever_flagged=listing.is_ever_flagged,
                        created_at=listing.created_at,
                        updated_at=listing.updated_at,
                        business_profile=business,  # reuse current business profile
                        is_reported=listing.is_reported
                    )
                    for listing in listings
                ]
            )

        except BusinessProfile.DoesNotExist:
            raise GraphQLError("Business profile not found")
        except Exception as e:
            raise GraphQLError(f"Error fetching business profile: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_channel_by_id(
        self,
        info: Info,
        channel_id: strawberry.ID,
        # "advertisement" or "business"
        view_by: Optional[str] = "advertisement"
    ) -> Optional[ChannelDetailsType]:
        try:
            channel = AdvertisementChannel.objects.get(id=channel_id)
            assignments = channel.advertisement_assignments.filter(
                advertisement__advertisement_status='active'
            ).prefetch_related(
                "advertisement__user__business_profile__business_hours",
                "advertisement__flags"
            )

            advertisements = [
                a.advertisement for a in assignments
                if not a.advertisement.is_flagged
            ]
            categories = list(
                channel.business_categories.values_list("name", flat=True))
            if view_by == "advertisement":
                ad_data = []
                for ad in advertisements:
                    bp = ad.user.business_profile if hasattr(
                        ad.user, "business_profile") else None
                    bh = bp.business_hours.all() if bp and hasattr(bp, "business_hours") else []

                    ad_data.append(
                        AdvertisementWithChannlesReturnType(
                            id=ad.id,
                            advertisment_name=ad.advertisment_name,
                            advertisment_description=ad.advertisment_description,
                            advertisement_image=ad.advertisement_image if hasattr(
                                ad, "advertisement_image") else [],
                            start_date=ad.start_date,
                            end_date=ad.end_date,
                            advertisment_cost=ad.advertisment_cost,
                            advertisement_status=ad.advertisement_status,
                            is_flagged=ad.is_flagged,
                            is_ever_flagged=ad.is_ever_flagged,
                            flag=ad.active_flag,

                            business_profile=BusinessProfileReturnType(
                                id=bp.id,
                                business_name=bp.business_name,
                                business_uuid=str(bp.business_uuid) if bp.business_uuid else None,
                                address=bp.address,
                                phone=bp.phone,
                                latitude=bp.latitude,
                                longitude=bp.longitude,
                                description=bp.description,
                                logo_url=bp.logo_url,
                                city=bp.city,
                                state=bp.state,
                                zip_code=bp.zip_code,
                                website=bp.website,
                                payment_methods=bp.payment_methods,
                                category=bp.category,
                                subcategories=bp.subcategories,
                                business_hours=[
                                    BusinessHoursReturnType(
                                        day=bhx.day,
                                        opening_time=str(
                                            bhx.opening_time) if bhx.opening_time else None,
                                        closing_time=str(
                                            bhx.closing_time) if bhx.closing_time else None,
                                        is_closed=bhx.is_closed
                                    ) for bhx in bh
                                ]
                            ) if bp else None
                        )
                    )

                return ChannelDetailsType(
                    id=channel.id,
                    channel_name=channel.channel_name,
                    city=channel.city,
                    province=channel.province,
                    locality=channel.locality,
                    latitude=channel.latitude,
                    longitude=channel.longitude,
                    price_per_day=channel.price_per_day,
                    apply_discount=channel.is_discount_active_today(),
                    discount_status_message=channel.get_discount_status_message(),
                    start_date=str(channel.start_date),
                    end_date=str(channel.end_date),
                    status=channel.status,
                    discount_price=channel.discount_price,
                    categories=categories,
                    channel_image=channel.channel_image,
                    advertisements=ad_data,
                    business_profile=None,
                    discount_history=build_discount_history(channel),
                )

            elif view_by == "business":
                business_profiles = {
                    ad.user.business_profile
                    for ad in advertisements
                    if hasattr(ad.user, "business_profile") and ad.user.business_profile
                }

                bp_data = []
                for bp in business_profiles:
                    bh = bp.business_hours.all() if hasattr(bp, "business_hours") else []
                    bp_data.append(
                        BusinessProfileReturnType(
                            id=bp.id,
                            business_name=bp.business_name,
                            business_uuid=str(bp.business_uuid) if bp.business_uuid else None,
                            address=bp.address,
                            phone=bp.phone,
                            latitude=bp.latitude,
                            longitude=bp.longitude,
                            description=bp.description,
                            logo_url=bp.logo_url,
                            cover_image=bp.cover_image,
                            city=bp.city,
                            state=bp.state,
                            zip_code=bp.zip_code,
                            website=bp.website,
                            payment_methods=bp.payment_methods,
                            category=bp.category,
                            subcategories=bp.subcategories,
                            business_hours=[
                                BusinessHoursReturnType(
                                    day=bhx.day,
                                    opening_time=str(
                                        bhx.opening_time) if bhx.opening_time else None,
                                    closing_time=str(
                                        bhx.closing_time) if bhx.closing_time else None,
                                    is_closed=bhx.is_closed
                                ) for bhx in bh
                            ]
                        )
                    )

                return ChannelDetailsType(
                    id=channel.id,
                    channel_name=channel.channel_name,
                    channel_image=channel.channel_image,
                    city=channel.city,
                    province=channel.province,
                    locality=channel.locality,
                    latitude=channel.latitude,
                    longitude=channel.longitude,
                    price_per_day=channel.price_per_day,
                    apply_discount=channel.is_discount_active_today(),
                    discount_status_message=channel.get_discount_status_message(),
                    start_date=str(channel.start_date),
                    end_date=str(channel.end_date),
                    status=channel.status,
                    discount_price=channel.discount_price,
                    advertisements=None,
                    business_profile=bp_data,
                    categories=categories,
                    discount_history=build_discount_history(channel),
                )

            else:
                raise ValueError(
                    "Invalid value for view_by. Must be 'advertisement' or 'business'.")

        except AdvertisementChannel.DoesNotExist:
            return None

    # @strawberry.field
    # @require_api_secret
    # @require_authentication
    # @jwt_required
    # def get_near_by_advertisements(
    #     self,
    #     info: Info,
    #     latitude: float,
    #     longitude: float,
    #     radius_in_km: float,
    #     category_ids: Optional[List[int]] = None,
    #     sort_by: Optional[str] = "newest"  # accepts "newest", "alphabetical", or "ending Soon"
    # ) -> List[AdvertisementWithChannlesReturnType]:
    #     try:
    #         ads = Advertisement.objects.select_related("user", "user__business_profile").all()

    #         # Filter by categories if provided
    #         if category_ids:
    #             ads = ads.filter(user__business_profile__category__id__in=category_ids).distinct()

    #         # Filter by distance
    #         nearby_ads = []
    #         for ad in ads:
    #             if not ad.user or not ad.user.business_profile:
    #                 continue
    #             profile = ad.user.business_profile
    #             if profile.latitude is None or profile.longitude is None:
    #                 continue

    #             distance_km = geodesic(
    #                 (latitude, longitude),
    #                 (profile.latitude, profile.longitude)
    #             ).km

    #             if distance_km <= radius_in_km:
    #                 nearby_ads.append((ad, distance_km))

    #         # Sorting
    #         if sort_by == "alphabetical":
    #             nearby_ads.sort(key=lambda item: item[0].advertisment_name.lower())
    #         elif sort_by == "ending Soon":
    #             nearby_ads.sort(key=lambda item: item[0].end_date or item[0].created_at)
    #         else:  # newest
    #             nearby_ads.sort(key=lambda item: item[0].created_at, reverse=True)

    #         # Return just the ad objects (not distance)
    #         return [ad for ad, _ in nearby_ads]

    #     except Exception as e:
    #         raise GraphQLError(f"Error fetching nearby advertisements: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_near_by_advertisements(
        self,
        info: Info,
        latitude: float,
        longitude: float,
        radius_in_km: float,
        category_ids: Optional[List[int]] = None,
        # "newest", "alphabetical", or "ending Soon"
        sort_by: Optional[str] = "newest"
    ) -> List[AdvertisementWithChannlesReturnType]:
        try:
            # Earth radius in km
            EARTH_RADIUS = 6371.0

            # Base query: only active, non-suspended, and not currently flagged ads
            ads = Advertisement.objects.filter(
                advertisement_status="active",
                user__business_profile__is_suspended=False,
                user__business_profile__is_deactivated=False,
                user__is_active=True
            ).exclude(
                flags__resolved=False
            ).select_related("user", "user__business_profile")

            # Filter by categories if provided
            if category_ids:
                ads = ads.filter(
                    user__business_profile__category__id__in=category_ids)

            # Add distance annotation using Haversine formula
            ads = ads.annotate(
                distance_km=EARTH_RADIUS * ACos(
                    Cos(Radians(latitude)) *
                    Cos(Radians(F("user__business_profile__latitude"))) *
                    Cos(Radians(F("user__business_profile__longitude")) - Radians(longitude)) +
                    Sin(Radians(latitude)) *
                    Sin(Radians(F("user__business_profile__latitude")))
                )
            )

            # Filter by radius
            ads = ads.filter(distance_km__lte=radius_in_km)
            for ad in ads:
                print("Ad:", ad.advertisment_name)
                print("User:", ad.user)
                if ad.user and hasattr(ad.user, "business_profile"):
                    print("Business Profile:", ad.user.business_profile)
                else:
                    print("Business Profile: None")
                print("-" * 50)
            # Sorting
            if sort_by == "alphabetical":
                ads = ads.order_by("advertisment_name")
            elif sort_by == "ending Soon":
                ads = ads.order_by(F("end_date").asc(nulls_last=True))
            else:  # newest
                ads = ads.order_by("-created_at")

            return [
                AdvertisementWithChannlesReturnType(
                    id=ad.id,
                    advertisment_name=ad.advertisment_name,
                    advertisment_description=ad.advertisment_description,
                    advertisement_image=ad.advertisement_image if hasattr(
                        ad, "advertisement_image") else [],
                    start_date=ad.start_date,
                    end_date=ad.end_date,
                    advertisment_cost=ad.advertisment_cost,
                    advertisement_status=ad.advertisement_status,
                    is_flagged=ad.is_flagged,
                    is_ever_flagged=ad.is_ever_flagged,
                    flag=ad.active_flag,
                    is_reported=ad.is_reported,


                    business_profile=(
                        BusinessProfileReturnType(
                            id=ad.user.business_profile.id,
                            business_name=ad.user.business_profile.business_name,
                            business_uuid=str(ad.user.business_profile.business_uuid) if ad.user.business_profile.business_uuid else None,
                            description=ad.user.business_profile.description,
                            logo_url=ad.user.business_profile.logo_url,
                            address=ad.user.business_profile.address,
                            address2=ad.user.business_profile.address2,
                            city=ad.user.business_profile.city,
                            state=ad.user.business_profile.state,
                            zip_code=ad.user.business_profile.zip_code,
                            latitude=ad.user.business_profile.latitude,
                            longitude=ad.user.business_profile.longitude,
                            phone=ad.user.business_profile.phone,
                            website=ad.user.business_profile.website,
                            payment_methods=ad.user.business_profile.payment_methods or [],
                            category=ad.user.business_profile.category,
                            subcategories=ad.user.business_profile.subcategories or [],
                            cover_image=ad.user.business_profile.cover_image,
                            business_hours=[
                                BusinessHoursReturnType(
                                    day=bh.day,
                                    opening_time=bh.opening_time,
                                    closing_time=bh.closing_time,
                                    is_closed=bh.is_closed
                                )
                                for bh in ad.user.business_profile.business_hours.all()
                            ]
                        )
                        if ad.user and hasattr(ad.user, "business_profile") else None
                    )
                )
                for ad in ads
            ]

        except Exception as e:
            raise GraphQLError(
                f"Error fetching nearby advertisements: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_near_by_listings(
        self,
        info: Info,
        latitude: float,
        longitude: float,
        radius_in_km: float,
        category_ids: Optional[List[int]] = None,
        sort_by: Optional[str] = "newest"
    ) -> List[ListingType]:
        try:
            EARTH_RADIUS = 6371.0

            # Base query
            qs = Listings.objects.filter(
                listing_status="active",
                user__business_profile__is_suspended=False,
                user__business_profile__is_deactivated=False,
                user__is_active=True,
                flag__isnull=True
            ).select_related("user", "user__business_profile")

            # Filter by categories
            if category_ids:
                qs = qs.filter(
                    user__business_profile__category__id__in=category_ids)

            # Distance annotation (Haversine formula)
            qs = qs.annotate(
                distance_km=EARTH_RADIUS * ACos(
                    Cos(Radians(latitude)) *
                    Cos(Radians(F("user__business_profile__latitude"))) *
                    Cos(
                        Radians(F("user__business_profile__longitude")) -
                        Radians(longitude)
                    ) +
                    Sin(Radians(latitude)) *
                    Sin(Radians(F("user__business_profile__latitude")))
                )
            )

            # Only listings within the radius
            qs = qs.filter(distance_km__lte=radius_in_km)

            # Sorting
            if sort_by == "alphabetical":
                qs = qs.order_by("listing_name")
            elif sort_by == "ending Soon":
                qs = qs.order_by(F("end_date").asc(nulls_last=True))
            else:  # default = newest
                qs = qs.order_by("-created_at")

            # Return data
            return [build_listing_response(listing) for listing in qs]

        except Exception as e:
            raise GraphQLError(f"Error fetching nearby listings: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_liked_business_profiles(
        self,
        info,
    ) -> List[BusinessProfileType]:
        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, "client_profile"):
                raise GraphQLError("User does not have a client profile.")

            profile = user.client_profile

            # Get liked business profiles
            liked_qs = LikedBusinessProfile.objects.filter(
                profile=profile,
                business_profile__is_suspended=False,
                business_profile__is_deactivated=False,
                business_profile__user__is_active=True
            )

            # Map to BusinessProfileType
            result = [
                BusinessProfileType.from_instance(liked.business_profile)
                for liked in liked_qs
            ]

            return result

        except Exception as e:
            raise GraphQLError(
                f"Error fetching liked business profiles: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_liked_advertisements(self, info: Info) -> List[AdvertisementWithChannlesReturnType]:
        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, "client_profile"):
                raise GraphQLError("User does not have a client profile.")

            client_profile: ClientProfile = user.client_profile

            # Get all liked advertisements that are active and not flagged
            liked_ads_qs = LikedAdvertisement.objects.filter(
                client_profile=client_profile,
                advertisement__advertisement_status="active",
                advertisement__user__business_profile__is_suspended=False,
                advertisement__user__business_profile__is_deactivated=False,
                advertisement__user__is_active=True
            ).exclude(
                advertisement__flags__resolved=False
            ).select_related(
                "advertisement", "advertisement__user", "advertisement__user__business_profile"
            )

            advertisements = []
            for liked in liked_ads_qs:
                ad = liked.advertisement
                business_profile_instance = getattr(
                    ad.user, "business_profile", None)

                # Build business hours if exists
                business_hours_list = []
                if business_profile_instance and hasattr(business_profile_instance, "business_hours"):
                    for bh in business_profile_instance.business_hours.all():
                        business_hours_list.append(
                            BusinessHoursReturnType(
                                day=bh.day,
                                opening_time=bh.opening_time.isoformat() if bh.opening_time else None,
                                closing_time=bh.closing_time.isoformat() if bh.closing_time else None,
                                is_closed=bh.is_closed
                            )
                        )
                business_profile_return = None
                if business_profile_instance:
                    business_profile_return = BusinessProfileReturnType(
                        id=business_profile_instance.id,
                        business_name=business_profile_instance.business_name,
                        business_uuid=str(business_profile_instance.business_uuid) if business_profile_instance.business_uuid else None,
                        description=business_profile_instance.description,
                        logo_url=business_profile_instance.logo_url,
                        address=business_profile_instance.address,
                        address2=business_profile_instance.address2,
                        city=business_profile_instance.city,
                        state=business_profile_instance.state,
                        zip_code=business_profile_instance.zip_code,
                        latitude=business_profile_instance.latitude,
                        longitude=business_profile_instance.longitude,
                        phone=business_profile_instance.phone,
                        website=business_profile_instance.website,
                        payment_methods=business_profile_instance.payment_methods or [],
                        category=business_profile_instance.category,
                        subcategories=business_profile_instance.subcategories or [],
                        business_hours=business_hours_list,
                        cover_image=business_profile_instance.cover_image,
                    )
                advertisements.append(
                    AdvertisementWithChannlesReturnType(
                        id=ad.id,
                        advertisment_name=ad.advertisment_name,
                        advertisment_description=ad.advertisment_description,
                        advertisement_image=ad.advertisement_image if hasattr(
                            ad, "advertisement_image") else [],
                        start_date=ad.start_date,
                        end_date=ad.end_date,
                        advertisment_cost=ad.advertisment_cost,
                        advertisement_status=ad.advertisement_status,
                        is_flagged=ad.is_flagged,
                        is_ever_flagged=ad.is_ever_flagged,
                        flag=ad.active_flag,
                        business_profile=business_profile_return
                    )
                )

            return advertisements

        except Exception as e:
            raise GraphQLError(
                f"Error fetching liked advertisements: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_liked_advertisement_channels(self, info: Info) -> List[AdvertisementChannelReturnType]:
        user = info.context.request.user
        client_profile = user.client_profile

        liked_channels = AdvertisementChannel.objects.filter(
            liked_by_clients__client_profile=client_profile,
            status="active"
        ).distinct()

        return liked_channels

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def all_categories(self, info) -> List[BusinessCategoryType]:
        categories = BusinessCategory.objects.prefetch_related(
            "subcategories",
            "business_profiles",
            "advertisements",  # 👈 direct relation thanks to related_name
        )
        print("Cat", categories.all())
        results = []
        for cat in categories:
            results.append(
                BusinessCategoryType(
                    id=cat.id,
                    name=cat.name,
                    created_at=cat.created_at,
                    # ✅ Subcategories
                    subcategories=[
                        BusinessSubCategoryType(id=sub.id, name=sub.name)
                        for sub in cat.subcategories.all()
                    ],

                    # ✅ Business profiles
                    businesses=[
                        BusinessProfileReturnType(
                            id=bp.id,
                            business_name=bp.business_name,
                            business_uuid=str(bp.business_uuid) if bp.business_uuid else None,
                            description=bp.description,
                            logo_url=bp.logo_url,
                            cover_image=bp.cover_image,
                            address=bp.address,
                            address2=bp.address2,
                            city=bp.city,
                            state=bp.state,
                            zip_code=bp.zip_code,
                            latitude=bp.latitude,
                            longitude=bp.longitude,
                            phone=bp.phone,
                            website=bp.website,
                            payment_methods=bp.payment_methods or [],
                            category=bp.category.name if bp.category else None,
                            subcategories=bp.subcategories or [],
                            business_hours=[
                                BusinessHoursType(
                                    day=bh.day,
                                    opening_time=str(
                                        bh.opening_time) if bh.opening_time else None,
                                    closing_time=str(
                                        bh.closing_time) if bh.closing_time else None,
                                    is_closed=bh.is_closed,
                                    is_overnight=bh.is_overnight,is_24hours=bh.is_24hours
                                )
                                for bh in bp.business_hours.all()
                            ],
                            created_at=bp.created_at
                        )
                        for bp in cat.business_profiles.all()
                    ],

                    # ✅ Advertisement channels (direct many-to-many)
                    channels=[
                        AdvertisementType(
                            id=ad.id,
                            channel_name=ad.channel_name,
                            city=ad.city,
                            province=ad.province,
                            locality=ad.locality,
                            latitude=ad.latitude,
                            longitude=ad.longitude,

                            price_per_day=float(ad.price_per_day),
                            apply_discount=ad.is_discount_active_today(),
                            discount_status_message=ad.get_discount_status_message(),
                            start_date=(ad.start_date),
                            end_date=(ad.end_date) if ad.end_date else None,
                            status=ad.status,
                            discount_price=ad.discount_price,
                            channel_image=ad.channel_image,
                            business_categories=[
                                bc.name for bc in ad.business_categories.all()],
                            sub_business_categories=ad.sub_business_categories,
                            discount_history=build_discount_history(ad),
                        )
                        for ad in cat.advertisements.all()
                    ],
                )
            )

        return results

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def category_by_id(
        self,
        info,
        cat_id: int
    ) -> Optional[BusinessCategoryType]:
        try:
            cat = BusinessCategory.objects.prefetch_related(
                "subcategories",
                "business_profiles",
                "advertisements",
            ).get(id=cat_id)
        except BusinessCategory.DoesNotExist:
            return None

        return BusinessCategoryType(
            id=cat.id,
            name=cat.name,
            created_at=cat.created_at,

            # ✅ Subcategories
            subcategories=[
                BusinessSubCategoryType(id=sub.id, name=sub.name)
                for sub in cat.subcategories.all()
            ],

            # ✅ Business profiles
            businesses=[
                BusinessProfileReturnType(
                    id=bp.id,
                    business_name=bp.business_name,
                    business_uuid=str(bp.business_uuid) if bp.business_uuid else None,
                    description=bp.description,
                    logo_url=bp.logo_url,
                    cover_image=bp.cover_image,
                    address=bp.address,
                    address2=bp.address2,
                    city=bp.city,
                    state=bp.state,
                    zip_code=bp.zip_code,
                    latitude=bp.latitude,
                    longitude=bp.longitude,
                    phone=bp.phone,
                    website=bp.website,
                    payment_methods=bp.payment_methods or [],
                    category=bp.category.name if bp.category else None,

                    subcategories=bp.subcategories or [],
                    business_hours=[
                        BusinessHoursType(
                            day=bh.day,
                            opening_time=str(
                                bh.opening_time) if bh.opening_time else None,
                            closing_time=str(
                                bh.closing_time) if bh.closing_time else None,
                            is_closed=bh.is_closed,is_overnight=bh.is_overnight,is_24hours=bh.is_24hours
                        )
                        for bh in bp.business_hours.all()
                    ],
                    created_at=bp.created_at
                )
                for bp in cat.business_profiles.all()
            ],

            # ✅ Advertisement channels
            channels=[
                AdvertisementType(
                    id=ad.id,
                    channel_name=ad.channel_name,
                    city=ad.city,
                    province=ad.province,
                    locality=ad.locality,
                    latitude=ad.latitude,
                    longitude=ad.longitude,
                    price_per_day=float(ad.price_per_day),
                    apply_discount=ad.is_discount_active_today(),
                    discount_status_message=ad.get_discount_status_message(),
                    start_date=ad.start_date,
                    end_date=ad.end_date if ad.end_date else None,
                    status=ad.status,
                    discount_price=ad.discount_price,
                    channel_image=ad.channel_image,
                    business_categories=[
                        bc.name for bc in ad.business_categories.all()],
                    sub_business_categories=ad.sub_business_categories,
                    discount_history=build_discount_history(ad),
                )
                for ad in cat.advertisements.all()
            ],
        )

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_subject_categories(self, info) -> List[str]:
        SUBJECTS = [
            "Account Login Issues",
            "Password Reset Request",
            "Account Suspension Inquiry",
            "Two-Factor Authentication (2FA) Issues",
            "Account Reactivation Request",
            "Update Business Information",
            "Business Location/Address Change",
            "Business Hours Update",
            "Category/Subcategory Assignment",
            "Business Profile Approval Status",

            "Advertisement Creation Issues",
            "Advertisement Not Appearing",
            "Advertisement Flagged/Rejected",
            "Edit/Delete Advertisement",
            "Advertisement Performance Concerns",

            "Channel Selection Issues",
            "Channel Pricing Inquiry",
            "Channel Assignment Request",
            "Temporary Channel Questions",

            "Payment Method Issues",
            "Billing Discrepancy",
            "Invoice Request",
            "Refund Request",
            "Prorated Charges Inquiry",
            "Payment Processing Failed",

            "Free Listing Creation Issues",
            "Free Listing Not Visible",
            "Free Listing Metrics Question",

            "Dashboard Access Issues",
            "Metrics Not Displaying",
            "Report Generation Problem",
            "Performance Data Questions",

            "Platform Error/Bug Report",
            "Map Integration Issues",
            "Image Upload Problems",
            "General Technical Support",

            "Content Guidelines Clarification",
            "Terms of Service Question",
            "Platform Policy Inquiry",

            "Feature Request",
            "General Inquiry",
        ]

        try:
            user = info.context.request.user
            if user.role != "business":
                # only admin could get subjects list
                raise GraphQLError(
                    "You are not authorized to view subject categories.")
            return SUBJECTS

        except Exception as e:
            raise GraphQLError(f"Error fetching subject categories: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_business_by_id(
        self,
        info: Info,
        userId: int,
        search: Optional[List[str]] = None
    ) -> Optional[BusinessProfileDetailsType]:
        try:
            business = (
                BusinessProfile.objects
                .select_related("user", "category")
                .prefetch_related(
                    "business_hours",
                    Prefetch(
                        "user__advertisements",
                        queryset=Advertisement.objects.prefetch_related(
                            "channel_assignments__advertisement_channel"
                        ),
                    ),
                    "user__listings"   # 👈 prefetch listings for this business
                )
                .get(user__id=userId)
            )

            user = business.user
            # print("ads", business.user.advertisements.all())
            # Filter advertisements if `search` terms provided
            advertisements = user.advertisements.all()

            for ad in advertisements:
                for assignment in ad.channel_assignments.all():
                    print("→", assignment.advertisement_channel)
            if search:
                query = Q()
                for term in search:
                    query |= Q(advertisment_name__icontains=term)
                advertisements = advertisements.filter(query)

            # Fetch listings
            listings = user.listings.all()

            return BusinessProfileDetailsType(
                id=business.id,
                business_name=business.business_name,
                description=business.description,
                logo_url=business.logo_url,
                cover_image=business.cover_image,
                is_suspended=business.is_suspended,
                hours=[
                    BusinessHoursType(
                        day=hour.day,
                        opening_time=hour.opening_time.isoformat() if hour.opening_time else None,
                        closing_time=hour.closing_time.isoformat() if hour.closing_time else None,
                        is_closed=hour.is_closed,
                        is_overnight=hour.is_overnight,is_24hours=hour.is_24hours
                    )
                    for hour in business.business_hours.all()
                ],
                location=LocationType(
                    address=business.address,
                    city=business.city,
                    state=business.state,
                    zip_code=business.zip_code,
                    latitude=business.latitude,
                    longitude=business.longitude,
                ),
                contact=ContactInfoType(
                    phone=business.phone,
                    website=business.website
                ),
                payment_methods=business.payment_methods or [],
                category=business.category.name if business.category else None,
                subcategories=business.subcategories or [],
                user=UserType(
                    email=user.email,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    role=user.role,
                    id=user.id,
                    created_at=user.created_at,
                    updated_at=user.updated_at,
                    is_active=user.is_active,
                    is_verified=user.is_verified,
                ),
                advertisements=[
                    AdvertisementReturnType(
                        id=ad.id,
                        advertisment_name=ad.advertisment_name,
                        advertisment_description=ad.advertisment_description,
                        advertisement_image=ad.advertisement_image or [],
                        start_date=ad.start_date,
                        end_date=ad.end_date,
                        advertisment_cost=ad.advertisment_cost,
                        advertisement_status=ad.advertisement_status,
                        is_flagged=ad.is_flagged,
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
                        ]
                    )
                    for ad in advertisements
                ],
                listings=[
                    ListingType(
                        id=listing.id,
                        listing_name=listing.listing_name,
                        listing_description=listing.listing_description,
                        listing_image=listing.listing_image or [],
                        listing_status=listing.listing_status,
                        is_ever_flagged=listing.is_ever_flagged,
                        created_at=listing.created_at,
                        updated_at=listing.updated_at,
                        business_profile=business  # reuse current business profile
                    )
                    for listing in listings
                ]
            )

        except BusinessProfile.DoesNotExist:
            raise GraphQLError("Business profile not found")
        except Exception as e:
            raise GraphQLError(f"Error fetching business profile: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_suspension_history(self, info, userId: int) -> List[SuspensionType]:
        try:
            business = (
                BusinessProfile.objects
                .get(user__id=userId)
            )
            suspensions_and_reactivations = Suspension.objects.filter(
                business=business).select_related("suspended_by", "reactivated_by").order_by("-start_date")
            print(list(suspensions_and_reactivations.values()))
            results = []
            for s in suspensions_and_reactivations:
                suspended_by_user = None
                if s.suspended_by:
                    suspended_by_user = UserType(
                        email=s.suspended_by.email,
                        first_name=s.suspended_by.first_name,
                        last_name=s.suspended_by.last_name,
                        role=s.suspended_by.role,
                        id=s.suspended_by.id,
                        created_at=s.suspended_by.created_at,
                        updated_at=s.suspended_by.updated_at,
                        is_active=s.suspended_by.is_active,
                        is_verified=s.suspended_by.is_verified,
                    )

                reactivated_by_user = None
                if s.reactivated_by:
                    reactivated_by_user = UserType(
                        email=s.reactivated_by.email,
                        first_name=s.reactivated_by.first_name,
                        last_name=s.reactivated_by.last_name,
                        role=s.reactivated_by.role,
                        id=s.reactivated_by.id,
                        created_at=s.reactivated_by.created_at,
                        updated_at=s.reactivated_by.updated_at,
                        is_active=s.reactivated_by.is_active,
                        is_verified=s.reactivated_by.is_verified,
                    )

                results.append(
                    SuspensionType(
                        id=s.id,
                        action=s.action,
                        note=s.note,
                        suspended_by=suspended_by_user,
                        reactivated_by=reactivated_by_user,
                        start_date=s.start_date,
                        end_date=s.end_date,
                        is_active=s.is_active,
                        suspension_reason=s.suspension_reason,
                        business_response=s.business_response,
                    )
                )

            return results

        except BusinessProfile.DoesNotExist:
            raise GraphQLError("Business profile not found")
        except Exception as e:
            raise GraphQLError(
                f"Error fetching business profile: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_reported_ads_for_business(self, info) -> list[ReportedAdsType]:
        try:
            user = info.context.request.user

            if not hasattr(user, 'role') or user.role != "business":
                raise GraphQLError(
                    "Only users with role 'business' are authorized")

            ads = Advertisement.objects.filter(user=user, is_ever_reported=True).annotate(
                reports_count=Count("reports")
            )

            results = []
            for ad in ads:

                results.append(
                    ReportedAdsType(
                        id=ad.id,
                        advertisement_title=ad.advertisment_name,
                        first_report_date=ad.first_report_date,
                        admin_actions=ad.admin_actions,
                        reports_count=ad.reports_count,
                    )
                )

            return results

        except Exception as e:
            raise GraphQLError(f"Error fetching reported ads: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_reported_listings_for_business(self, info) -> list[ReportedListingsType]:
        try:
            user = info.context.request.user
            if not hasattr(user, 'role') or user.role != "business":
                raise GraphQLError(
                    "Only users with role 'business' are authorized")

            listings = Listings.objects.filter(user=user, is_ever_reported=True).annotate(
                reports_count=Count("reports")
            )
            results = []
            for listing in listings:
                results.append(
                    ReportedListingsType(id=listing.id,
                                         listing_title=listing.listing_name,
                                         first_report_date=listing.first_report_date,
                                         admin_actions=listing.admin_actions,
                                         reports_count=listing.reports_count,
                                         )
                )
            return results

        except Exception as e:
            raise GraphQLError(f"Error fetching reported ads: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_reports_for_ad(self, info, adId: int, status: Optional[str] = None) -> list[ReportsResponseType]:
        try:
            user = info.context.request.user

            if not hasattr(user, 'role') or user.role not in ["business", "admin"]:
                raise GraphQLError(
                    "Only users with role 'business' or 'admin' are authorized")

            if user.role != "admin":
                ad = Advertisement.objects.filter(id=adId, user=user).first()
                if not ad:
                    raise GraphQLError(
                        "You are not authorized to view reports for this advertisement.")

            query = AdvertisementReports.objects.filter(
                advertisement_id=adId
            ).select_related("user").order_by("-created_at")

            if status:
                query = query.filter(status=status)

            results = []
            for report in query:
                results.append(
                    ReportsResponseType(id=report.id,
                                        date_created=report.created_at,
                                        reason=report.reason,
                                        report_id=report.report_id,
                                        user_name=f"{report.user.first_name} {report.user.last_name}",
                                        status=report.status
                                        )
                )

            return results

        except Exception as e:
            raise GraphQLError(f"Error fetching reports: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_reports_for_listings(self, info, listingId: int, status: Optional[str] = None) -> list[ReportsResponseType]:
        try:
            user = info.context.request.user

            if not hasattr(user, 'role') or user.role not in ["business", "admin"]:
                raise GraphQLError(
                    "Only users with role 'business' are authorized")

            if user.role != "admin":
                listings = Listings.objects.filter(
                    id=listingId, user=user).first()
                if not listings:
                    raise GraphQLError(
                        "You are not authorized to view reports for this listing.")

            query = ListingReports.objects.filter(
                listing=listingId).order_by("-created_at")

            if status:
                query = query.filter(status=status)

            results = []
            for report in query:
                results.append(
                    ReportsResponseType(
                        id=report.id,
                        report_id=report.report_id,
                        date_created=report.created_at,
                        reason=report.reason,
                        user_name=f"{report.user.first_name} {report.user.last_name}",
                        status=report.status
                    )
                )

            return results

        except Exception as e:
            raise GraphQLError(f"Error fetching reports: {str(e)}")
    # for admin

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_all_reported_items(self, info, type: str) -> list[ReportedItemsType]:

        try:
            user = info.context.request.user

            if not hasattr(user, 'role') or user.role != "admin":
                raise GraphQLError(
                    "Only users with role 'admin' are authorized")

            results = []
            last_week = timezone.now() - timedelta(days=7)

            if type == "ad" or type == "all":
                ads = Advertisement.objects.filter(is_ever_reported=True).select_related(
                    "user__business_profile"
                ).annotate(
                    reports_count=Count("reports"),
                    reports_per_week=Count("reports", filter=Q(reports__created_at__gte=last_week))
                )

                for ad in ads:
                    business_profile = getattr(
                        ad.user, "business_profile", None)
                    business_name = business_profile.business_name if business_profile else None

                    results.append(
                        ReportedItemsType(
                            id=ad.id,
                            title=ad.advertisment_name,
                            first_report_date=ad.first_report_date,
                            admin_actions=ad.admin_actions,
                            reports_count=ad.reports_count,
                            reports_per_week=ad.reports_per_week,
                            business_name=business_name,
                            escalations=ad.escalation,
                            type="Advertisement"
                        )
                    )

            if type == "listing" or type == "all":
                listings = Listings.objects.filter(is_ever_reported=True).select_related(
                    "user__business_profile"
                ).annotate(
                    reports_count=Count("reports"),
                    reports_per_week=Count("reports", filter=Q(reports__created_at__gte=last_week))
                )
         
                for listing in listings:
                    business_profile = getattr(
                        listing.user, "business_profile", None)
                    business_name = business_profile.business_name if business_profile else None

                    results.append(
                        ReportedItemsType(
                            id=listing.id,
                            title=listing.listing_name,
                            first_report_date=listing.first_report_date,
                            admin_actions=listing.admin_actions,
                            reports_count=listing.reports_count,
                            reports_per_week=listing.reports_per_week,
                            business_name=business_name,
                            escalations=listing.escalation,
                            type="Listing"
                        )
                    )

            if not type in ["ad", "listing", "all"]:
                raise GraphQLError("Invalid type. Must be 'ad', 'listing', or 'all'.")
            return results
        except Exception as e:
            raise GraphQLError(f"Error fetching reports: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_all_users_with_reports(self, info) -> list[UserReportsType]:
        try:
            user = info.context.request.user

            if not hasattr(user, "role") or user.role != "admin":
                raise GraphQLError(
                    "Only users with role 'admin' are authorized")

            last_week = timezone.now() - timedelta(days=7)
            users = User.objects.filter(role="user").annotate(
                ads_reports_count=Count("reportedAds", filter=Q(reportedAds__status="pending"), distinct=True),
                listings_reports_count=Count("reportedListings", filter=Q(reportedListings__status="pending"), distinct=True),
                ads_reports_per_week=Count(
                    "reportedAds", filter=Q(reportedAds__status="pending", reportedAds__created_at__gte=last_week), distinct=True),
                listings_reports_per_week=Count(
                    "reportedListings", filter=Q(reportedListings__status="pending", reportedListings__created_at__gte=last_week), distinct=True),
            ).annotate(
                total_reports_count=F("ads_reports_count") + F("listings_reports_count"),
                total_reports_per_week=F("ads_reports_per_week") + F("listings_reports_per_week")
            ).filter(total_reports_count__gt=0)

            results = [
                UserReportsType(
                    user_name=f"{u.first_name} {u.last_name}",
                    reports_count=u.total_reports_count,  # ✅ total ads + listings
                    reports_per_week=u.total_reports_per_week,
                    user_id=u.id
                )
                for u in users
            ]

            return results
        except Exception as e:
            raise GraphQLError(f"Error fetching user reports: {str(e)}")

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_cat_and_subcat_by_id(self, info, id: str)->BusinessCategoryType:
        try:
            category = (
                BusinessCategory.objects
                .prefetch_related("subcategories")
                .get(pk=id)
            )
            return BusinessCategoryType(
                id=category.id,
                name=category.name,
                subcategories=[
                    BusinessSubCategoryType(
                        id=sub.id,
                        name=sub.name,
                    )
                    for sub in category.subcategories.all()
                ],
                created_at=category.created_at
            )

        except BusinessCategory.DoesNotExist:
            return None

        except Exception as e:
            raise GraphQLError(f"Error fetching category: {str(e)}")

    # ── Notification Queries ──────────────────────────────────────────────

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_notifications(
        self,
        info: Info,
        limit: int = 20,
        offset: int = 0,
    ) -> List[NotificationType]:
        """Paginated list of notifications for the logged-in user."""
        from api.models import Notification

        user = info.context.request.user
        queryset = Notification.objects.filter(recipient=user)

        if user.role == "user":
            from api.models import LikedAdvertisementChannel, LikedAdvertisement
            liked_channel_ids = list(LikedAdvertisementChannel.objects.filter(client_profile__user=user).values_list("advertisement_channel_id", flat=True))
            liked_ad_ids = list(LikedAdvertisement.objects.filter(client_profile__user=user).values_list("advertisement_id", flat=True))
            
            allowed_types = [
                "channel_phaseout", "discount_start", "discount_started", 
                "discount_ended", "discount_ending", "discount_available",
                "business_approved", "account_suspended", "ad_phased_out", "account_unsuspended"
            ]
            
            from django.db.models import Q
            queryset = queryset.filter(notification_type__in=allowed_types)
            queryset = queryset.filter(
                Q(channel_id__isnull=True, advertisement_id__isnull=True) |
                Q(channel_id__in=liked_channel_ids) |
                Q(advertisement_id__in=liked_ad_ids)
            )

        notifications = queryset.order_by("-created_at")[offset : offset + limit]

        return [
            NotificationType(
                id=n.id,
                notification_type=n.notification_type,
                title=n.title,
                message=n.message,
                is_read=n.is_read,
                recipient_role=n.recipient_role,
                advertisement_id=n.advertisement_id,
                channel_id=n.channel_id,
                invoice_id=n.invoice_id,
                created_at=n.created_at,
            )
            for n in notifications
        ]

    @strawberry.field
    @require_api_secret
    @require_authentication
    @jwt_required
    def get_unread_notification_count(self, info: Info) -> int:
        """Returns the number of unread notifications for the logged-in user."""
        from api.models import Notification

        user = info.context.request.user
        queryset = Notification.objects.filter(recipient=user, is_read=False)

        if user.role == "user":
            from api.models import LikedAdvertisementChannel, LikedAdvertisement
            liked_channel_ids = LikedAdvertisementChannel.objects.filter(client_profile__user=user).values_list("advertisement_channel_id", flat=True)
            liked_ad_ids = LikedAdvertisement.objects.filter(client_profile__user=user).values_list("advertisement_id", flat=True)
            
            allowed_types = [
                "channel_phaseout", "discount_start", "discount_started", 
                "discount_ended", "discount_ending", "discount_available",
                "business_approved", "account_suspended", "ad_phased_out", "account_unsuspended"
            ]
            
            from django.db.models import Q
            queryset = queryset.filter(notification_type__in=allowed_types)
            queryset = queryset.filter(
                Q(channel_id__isnull=True, advertisement_id__isnull=True) |
                Q(channel_id__in=liked_channel_ids) |
                Q(advertisement_id__in=liked_ad_ids)
            )

        return queryset.count()
