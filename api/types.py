from decimal import Decimal
import uuid
import strawberry
from strawberry.types import Info
from strawberry.file_uploads import Upload
from datetime import datetime, date, time
from typing import Optional, List
from .models import Profile
from datetime import datetime
import pytz


@strawberry.type
class ProfileType:
    gender: Optional[str]
    date_of_birth: Optional[date]



@strawberry.type
class BusinessHoursType:
    day: str
    opening_time: Optional[str]
    closing_time: Optional[str]
    is_closed: bool
    is_24hours: bool
    is_overnight: bool

@strawberry.type
class LocationType:
    address: str
    city: str
    state: str
    zip_code: str
    latitude: Optional[float]
    longitude: Optional[float]
    timezoneId: Optional[str] = None


@strawberry.type
class ContactInfoType:
    phone: str
    website: Optional[str]


@strawberry.type
class ChannelType:
    id: strawberry.ID
    channel_name: str

    city: Optional[str] = None
    province: Optional[str] = None
    locality: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    price_per_day: float
    apply_discount: bool
    start_date: str
    end_date: str
    status: str
    discount_price: float | None = None
    discount_status_message: Optional[str] = None
    channel_image: Optional[str] = None  # New field for channel image URL


@strawberry.type
class ClientProfileType:
    id: strawberry.ID = None
    gender: Optional[str] = None
    profession: Optional[str] = None
    interests: Optional[list[str]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    selected_channels: Optional[list[ChannelType]] = None
    date_of_birth: Optional[date] = None


@strawberry.input
class UpdateClientProfileInput:
    gender: Optional[str] = None
    profession: Optional[str] = None
    interests: Optional[List[str]] = None
    selected_channels: Optional[List[int]] = None


@strawberry.type
class BusinessProfileType:
    id: Optional[strawberry.ID] = None
    business_uuid: Optional[uuid.UUID] = None
    business_name: str
    description: Optional[str]
    logo_url: Optional[str]
    cover_image: Optional[str] = None
    hours: List[BusinessHoursType]
    location: LocationType
    contact: ContactInfoType
    payment_methods: List[str]
    category: Optional[str] = None  # Category name
    subcategories: List[str]
    listings: Optional[List["ListingType"]] = None

    @classmethod
    def from_instance(cls, instance, listings=None):

        # Get current time in UTC
        from datetime import timezone
        now = datetime.now(timezone.utc)
        today = now.strftime('%A').lower()  # e.g., 'monday'
        current_time = now.time()

        # Prepare hours list with dynamic is_closed update for today
        dynamic_hours = []
        for hour in instance.business_hours.all():
            is_closed = hour.is_closed
            if hour.day == today:
                if not is_closed and hour.opening_time and hour.closing_time:
                    # Recalculate is_closed based on current time
                    if not (hour.opening_time <= current_time <= hour.closing_time):
                        is_closed = True  # Currently closed even if normally open

            dynamic_hours.append(
                BusinessHoursType(
                    day=hour.day,
                    opening_time=hour.opening_time.isoformat() if hour.opening_time else None,
                    closing_time=hour.closing_time.isoformat() if hour.closing_time else None,
                    is_closed=is_closed,
                    is_24hours=hour.is_24hours or False,
                    is_overnight=hour.is_overnight or False
                )
            )

        return cls(
            id=instance.id,
            business_uuid=instance.business_uuid,
            business_name=instance.business_name,
            description=instance.description,
            logo_url=instance.logo_url,
            cover_image=instance.cover_image,
            hours=dynamic_hours,
            location=LocationType(
                address=instance.address,
                city=instance.city,
                state=instance.state,
                zip_code=instance.zip_code,
                latitude=instance.latitude,
                longitude=instance.longitude,
                timezoneId=getattr(instance, 'timezone_id', 'America/Toronto')
            ),
            contact=ContactInfoType(
                phone=instance.phone,
                website=instance.website
            ),
            payment_methods=instance.payment_methods,
            category=instance.category.name if instance.category else None,
            subcategories=instance.subcategories or [],
            listings=listings,
        )


@strawberry.type
class UserType:
    id: strawberry.ID
    email: str
    first_name: str
    last_name: str
    role: str
    created_at: Optional[datetime] = None  # or Optional[datetime]
    updated_at: Optional[datetime] = None
    profile: Optional[ProfileType] = None
    clientProfile: Optional[ClientProfileType] = None
    business_profile: Optional["BusinessUserProfileType"] = None
    is_active: Optional[bool]
    is_verified: Optional[bool] = None
    is_admin_approved: Optional[bool] = None
    personal_phone: Optional[str] = None


@strawberry.type
class SuspensionType:
    id: int
    suspension_reason: Optional[str] = None
    note: Optional[str]
    start_date: datetime
    end_date: Optional[datetime]
    action: Optional[str] = None
    is_active: Optional[bool] = True
    # Related models
    suspended_by: Optional[UserType] = None
    reactivated_by: Optional[UserType] = None
    business: Optional[BusinessProfileType] = None
    business_response: Optional[str] = None


@strawberry.type
class ReportedAdsType:
    id: int
    advertisement_title: str
    first_report_date: datetime
    admin_actions: str
    reports_count: int
    business_name: Optional[str] = None
    date_reviewed: Optional[datetime] = None
    escalations: Optional[int] = None


@strawberry.type
class ReportedItemsType:
    id: int
    title: str
    first_report_date: datetime
    admin_actions: str
    reports_count: int
    reports_per_week: int
    business_name: Optional[str] = None
    date_reviewed: Optional[datetime] = None
    escalations: Optional[bool] = None
    type: Optional[str] = None


@strawberry.type
class ReportedListingsType:
    id: int
    listing_title: str
    first_report_date: datetime
    admin_actions: str
    reports_count: int
    business_name: Optional[str] = None
    date_reviewed: Optional[datetime] = None
    escalations: Optional[int] = None


@strawberry.type
class UserReportsType:
    user_name: str
    reports_count: int
    reports_per_week: int
    user_id: int


@strawberry.type
class ReportsResponseType:
    id: str
    reason: str
    date_created: datetime
    report_id: Optional[str] = None
    user_name: str
    status: str = "pending"


@strawberry.type
class RevertSuspendedBusinessReturnType:
    success: bool
    message: str


@strawberry.type
class BusinessUserProfileType:
    business_uuid: Optional[uuid.UUID] = None
    business_name: str
    description: Optional[str]
    logo_url: Optional[str]
    address: Optional[str]
    city: str
    state: str
    zip_code: str
    latitude: Optional[float]
    longitude: Optional[float]
    phone: str
    website: Optional[str]
    payment_methods: Optional[list[str]] = None
    subcategories: Optional[list[str]] = None
    category_id: Optional[int]
    cover_image: Optional[str] = None
    timezoneId: Optional[str] = None
    suspension: Optional[SuspensionType] = None
    hours: Optional[list[BusinessHoursType]] = None  # ✅ fix here
    is_suspended: Optional[bool] = None


@strawberry.type
class LocationSaveRespone:
    success: bool
    message: Optional[str]


@strawberry.type
class LikedRespone:
    success: bool
    message: Optional[str]


@strawberry.type
class UnsaveAllBusinessProfilesResponse:
    success: bool
    message: str
    count: int


@strawberry.type
class DetailsType:
    key: str


@strawberry.type
class LoginResponse:
    id: int
    email: str
    first_name: str
    last_name: str
    access_token: str
    refresh_token: str
    is_active: bool
    is_verified: bool
    is_admin_approved: bool
    role: str


@strawberry.type
class ResendOTPResponse:
    success: bool
    message: Optional[str]


@strawberry.type
class ForgotPasswordResponse:
    success: bool
    user_id: Optional[int] = None
    message: Optional[str] = None


@strawberry.type
class ResetPasswordResponse:
    success: bool


@strawberry.input
class ChangePasswordInput:
    current_password: str
    new_password: str


@strawberry.type
class AuthPayload:
    success: bool
    message: str
    user: Optional[UserType]
    token: Optional[str]


# inputs


@strawberry.input
class RegisterInput:
    email: str
    password: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: str  # "user" or "business"

    # User fields
    gender: Optional[str] = None
    date_of_birth: Optional[date] = None

    # Business fields
    business_name: Optional[str] = None
    business_address: Optional[str] = None
    business_city: Optional[str] = None
    business_province: Optional[str] = None
    business_postal_code: Optional[str] = None
    logo_url: Optional[str] = None
    phone: Optional[str] = None
    
    business_description: Optional[str] = None

    # Account holder's own phone (not tied to the business)
    personal_phone: Optional[str] = None


@strawberry.input
class RegisterClientInput:
    email: str
    password: str
    first_name: str
    last_name: str
    date_of_birth: Optional[date] = None


@strawberry.input
class LoginInput:
    email: str
    password: str


@strawberry.input
class BusinessHoursUpdateInput:
    id: strawberry.ID
    day: Optional[str] = strawberry.UNSET
    opening_time: Optional[str] = strawberry.UNSET
    closing_time: Optional[str] = strawberry.UNSET
    is_closed: Optional[bool] = strawberry.UNSET
    is_24hours: Optional[bool] = strawberry.UNSET
    is_overnight: Optional[bool] = strawberry.UNSET


@strawberry.input
class BusinessHoursInput:
    day: str
    opening_time: Optional[str] = None
    closing_time: Optional[str] = None
    is_closed: bool = False
    is_24hours: Optional[bool] = strawberry.field(default=False, name="is24hours")
    is_overnight: Optional[bool] = strawberry.field(default=False, name="isOvernight")


@strawberry.input
class LocationInput:
    address: str
    address2: Optional[str] = None
    city: str
    state: str
    zip_code: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezoneId: Optional[str] = None


@strawberry.input
class ContactInfoInput:
    phone: str
    website: Optional[str] = None


@strawberry.input
class BusinessProfileUpdateInput:
    business_name: Optional[str] = None
    description: Optional[str] = None
    hours: Optional[List[BusinessHoursInput]] = None
    location: Optional[LocationInput] = None
    contact: Optional[ContactInfoInput] = None
    payment_methods: Optional[List[str]] = None
    category_id: Optional[strawberry.ID] = None  # Now a single ID
    subcategories: Optional[List[str]] = None  # New field
    logo: Optional[str] = None
    cover_image: Optional[str] = None


@strawberry.input
class UpdateAccountHolderInput:
    first_name: str
    last_name: str


@strawberry.input
class AddCategoryInput:
    name: str
    subcategory: Optional[List[str]] = None


@strawberry.input
class UpdateCategoryInput:
    id: strawberry.ID
    name: str
    subcategory: Optional[List[str]] = None


@strawberry.type
class BusinessHoursReturnType:
    day: str
    opening_time: Optional[str]
    closing_time: Optional[str]
    is_closed: bool
    is_24hours: Optional[bool] = None
    is_overnight: Optional[bool] = None


@strawberry.type
class BusinessProfileReturnType:
    id: Optional[strawberry.ID] = None
    business_uuid: Optional[str] = None
    business_name: str
    description: Optional[str]
    logo_url: Optional[str]
    address: str
    address2: Optional[str] = None
    city: str
    state: str
    zip_code: str
    latitude: Optional[float]
    longitude: Optional[float]
    timezoneId: Optional[str] = None
    phone: str
    website: Optional[str]
    payment_methods: List[str]
    category: Optional[str] = None  # Updated from List[str] to single str
    subcategories: Optional[List[str]] = None  # New field
    business_hours: Optional[List[BusinessHoursReturnType]] = None
    cover_image: Optional[str] = None
    created_at: Optional[datetime] = None
    is_suspended: Optional[bool] = None


@strawberry.input
class CreateSuspensionInput:
    business_id: int
    reason: str
    note: Optional[str] = None


@strawberry.input
class CreateAdvertisementInput:
    channel_name: str
    business_category: List[int]
    sub_business_categories: Optional[List[str]] = None
    city: Optional[str] = None
    province: Optional[str] = None
    locality: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    price_per_day: float
    apply_discount: bool = False
    discount_price: Optional[float] | None = None
    discount_start_date: Optional[date] = None
    discount_end_date: Optional[date] = None
    start_date: date
    end_date: Optional[date] = None
    expiry_at: Optional[datetime] = None  # Exact UTC expiry
    channel_image: Optional[str] = None  # New field for channel image URL
    timezone: Optional[str] = "America/Toronto"  # IANA timezone string
    reused_from_id: Optional[int] = None


@strawberry.type
class SuccessAdvertisementCreatedType:
    id: strawberry.ID
    message: str


@strawberry.type
class SuccessEndListingType:
    id: strawberry.ID
    message: str


@strawberry.type
class AdvertisementFlagType:
    id: strawberry.ID
    title: Optional[str]
    description: Optional[str]
    images: Optional[List[str]]
    flagged_at: datetime
    resolved: bool


@strawberry.type
class ListingFlagType:
    id: strawberry.ID
    title: Optional[str]
    description: Optional[str]
    images: Optional[List[str]]
    flagged_at: datetime
    resolved: bool


@strawberry.input
class FlagAdvertisementInput:
    advertisement_id: strawberry.ID
    reason: str


@strawberry.type
class ChannelDiscountHistoryType:
    id: strawberry.ID
    apply_discount: bool
    discount_price: Optional[float] = None
    discount_status_message: Optional[str] = None
    effective_from: date
    effective_until: Optional[date] = None


@strawberry.type
class AdvertisementType:
    id: strawberry.ID
    channel_name: str
    business_categories: list[str]
    sub_business_categories: list[str]
    city: Optional[str] = None
    province: Optional[str] = None
    locality: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    price_per_day: float
    apply_discount: bool
    start_date: str
    end_date: Optional[str] = None
    status: str
    discount_price: float | None = None
    discount_status_message: Optional[str] = None
    discount_start_date: Optional[date] = None
    discount_end_date: Optional[date] = None
    channel_image: Optional[str] = None  # New field for channel image URL
    pending_apply_discount: Optional[bool] = None
    pending_discount_price: Optional[float] = None
    pending_discount_start_date: Optional[date] = None
    pending_discount_end_date: Optional[date] = None
    discount_effective_from: Optional[datetime] = None
    discount_history: Optional[list[ChannelDiscountHistoryType]] = None
    timezone: Optional[str] = None  # IANA timezone string

    @strawberry.field
    def is_flagged(self) -> bool:
        return hasattr(self, "flag")

    @strawberry.field
    def flag(self) -> Optional[AdvertisementFlagType]:
        if hasattr(self, "flag"):
            return self.flag
        return None


@strawberry.input
class UpdateAdvertisementChannelInput:
    id: int
    channel_name: Optional[str] = None
    business_category: Optional[List[int]] = None
    sub_business_categories: Optional[List[str]] = None
    city: Optional[str] = None
    province: Optional[str] = None
    locality: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    price_per_day: Optional[float] = None
    apply_discount: Optional[bool] = None
    discount_price: Optional[float] = None
    discount_start_date: Optional[date] = strawberry.UNSET
    discount_end_date: Optional[date] = strawberry.UNSET
    start_date: Optional[date] = None
    end_date: Optional[date] = strawberry.UNSET
    expiry_at: Optional[datetime] = strawberry.UNSET  # Exact UTC expiry
    channel_image: Optional[str] = None  # New field for channel image URL
    timezone: Optional[str] = None  # IANA timezone string — None means no change


@strawberry.input
class ChangeAdvertisementStatusInput:
    advertisement_id: int
    status: str  # Expected values: "active", "disable", etc.


@strawberry.input
class ApproveUserInput:
    user_id: int


@strawberry.type
class SuccessUserApprovalType:
    id: strawberry.ID
    message: str


@strawberry.input
class AdvertisementInput:
    advertisment_name: str
    advertisment_description: Optional[str] = ""
    advertisement_image: Optional[List[str]] = None
    start_date: date
    end_date: Optional[date] = None
    expiry_at: Optional[datetime] = None  # Exact UTC expiry
    timezone: Optional[str] = "America/Toronto"  # IANA timezone string
    advertisment_cost: Decimal
    advertisement_status: Optional[str] = "active"
    reused_from_id: Optional[int] = None
    channel_ids: List[int]  # IDs of AdvertisementChannelAssignment
    duration_in_weeks: Optional[int] = None
    payment_method_id: Optional[str] = None


@strawberry.input
class CreateListingInput:
    listing_name: str
    listing_description: Optional[str] = ""
    listing_image: Optional[List[str]] = None
    listing_status: Optional[str] = "active"


@strawberry.type
class ListingSuccessType:
    id: int
    message: str


@strawberry.type
class ListingType:
    id: int
    listing_name: str
    listing_description: Optional[str]
    listing_image: Optional[List[str]]
    listing_status: Optional[str] = "active"
    is_ever_flagged: bool
    created_at: datetime
    updated_at: datetime
    business_profile: Optional[BusinessProfileReturnType] = None
    flag: Optional[ListingFlagType] = None
    is_reported: Optional[bool] = None
    is_flagged: Optional[bool] = None


@strawberry.input
class UpdateListingInput:
    id: int
    listing_name: Optional[str] = None
    listing_description: Optional[str] = None
    listing_image: Optional[List[str]] = None
    listing_status: Optional[str] = "active"


@strawberry.input
class AddCategoryInput:
    cat: str
    sub: List[str]


@strawberry.input
class UpdateCategoryInput:
    id: int              # The category to update
    cat: Optional[str] = None        # Optional new category name
    sub: Optional[List[str]] = None  # Optional new subcategories

@strawberry.type
class AdvertisementSuccessType:
    id: int
    message: str
    clientSecret: Optional[str] = None


@strawberry.input
class UpdateAdvertisementInput:
    id: int
    advertisment_name: Optional[str] = None
    advertisment_description: Optional[str] = None
    advertisement_image: Optional[List[str]] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    expiry_at: Optional[datetime] = None
    timezone: Optional[str] = None
    advertisment_cost: Optional[Decimal] = None
    advertisement_status: Optional[str] = None
    channel_ids: Optional[List[int]] = None


@strawberry.input
class ChangeAdvertisementStatusInput:
    id: int
    status: str


@strawberry.type
class AdvertisementChannelType:
    id: int
    channel_name: str
    city: Optional[str]
    province: Optional[str]
    locality: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    price_per_day: float
    apply_discount: bool
    discount_start_date: Optional[date] = None
    discount_end_date: Optional[date] = None
    start_date: date
    discount_price: Optional[float] = None
    discount_status_message: Optional[str] = None
    end_date: Optional[date] = None
    channel_image: Optional[str] = None
    discount_history: Optional[List["ChannelDiscountHistoryType"]] = None


@strawberry.type
class BusinessSubCategoryType:
    id: int
    name: str


@strawberry.type
class BusinessCategoryType:
    id: int
    name: str
    subcategories: Optional[List["BusinessSubCategoryType"]] = None
    businesses: Optional[List["BusinessProfileReturnType"]] = None
    channels: Optional[List["AdvertisementType"]] = None
    created_at: datetime

    # @strawberry.field
    # def subcategories(self) -> List["BusinessSubCategoryType"]:
    #     return list(self.subcategories.all())


@strawberry.type
class CreateCategoryAndSubCategorySuccessType:
    message: str
    success: str
@strawberry.type
class UpdateCategoryAndSubCategorySuccessType:
    message: str
    success: str

@strawberry.type
class AdvertisementReturnType:
    id: int
    advertisment_name: str
    advertisment_description: Optional[str]
    advertisement_image: List[str]
    start_date: date
    end_date: Optional[date]
    advertisment_cost: float
    advertisement_status: str
    reused_from_id: Optional[int] = None
    channel_assignments: List[AdvertisementChannelType]
    is_flagged: bool
    flag: Optional[AdvertisementFlagType] = None
    business_profile: Optional[BusinessProfileReturnType] = None
    is_ever_flagged: Optional[bool] = None
    is_reported: Optional[bool] = None
    admin_actions: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    last_billed_at: Optional[datetime] = None
    billing_status: Optional[str] = None
    total_revenue: Optional[float] = 0.0
    actual_cost: Optional[float] = 0.0
    has_been_reused: bool = False


@strawberry.type
class PaymentMethodCardType:
    brand: str
    last4: str
    exp_month: int
    exp_year: int

@strawberry.type
class PaymentMethodType:
    id: str
    card: Optional[PaymentMethodCardType] = None
    created: int


@strawberry.type
class ChannelAdvertisementReturnType:
    id: int
    advertisment_name: str
    advertisment_description: Optional[str]
    advertisement_image: List[str]
    start_date: date
    end_date: Optional[date]
    advertisment_cost: float
    advertisement_status: str
    is_flagged: bool
    is_ever_flagged: Optional[bool] = None
    flag: Optional[AdvertisementFlagType] = None
    is_reported: bool
    has_been_reused: bool = False


@strawberry.type
class ChannelWithAdsType:
    id: int
    channel_name: str
    city: Optional[str]
    province: Optional[str]
    locality: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    price_per_day: Decimal
    apply_discount: bool
    discount_price: Optional[Decimal]
    discount_status_message: Optional[str] = None
    discount_start_time: Optional[time] = None
    discount_end_time: Optional[time] = None
    start_date: date
    end_date: Optional[date]
    status: str
    timezone: Optional[str] = None  # IANA timezone string
    sub_business_categories: Optional[List[str]] = None
    channel_image: Optional[str] = None
    advertisements: Optional[List[ChannelAdvertisementReturnType]] = None
    business_categories: Optional[List[BusinessCategoryType]] = None
    business_profile: Optional[BusinessProfileReturnType] = None
    discount_history: Optional[List[ChannelDiscountHistoryType]] = None


@strawberry.type
class MonthlyInvoiceType:
    id: strawberry.ID
    start_date: date
    end_date: date
    total_amount: float
    status: str
    stripe_payment_intent_id: Optional[str]
    created_at: datetime


@strawberry.type
class BillingSummaryType:
    current_month_cost: float
    billing_period_start: date
    invoices: List[MonthlyInvoiceType]


@strawberry.type
class ChannelDailySpendType:
    channel_name: str
    price_per_day: float
    discount: float
    cost: float
    flagged: bool


@strawberry.type
class DayBillingType:
    date: date
    channels: List[str]
    total_cost: float
    is_flagged: bool
    details: List[ChannelDailySpendType]


@strawberry.input
class ReportAdvertisementInput:
    advertisement_id: strawberry.ID
    title: Optional[str] = None
    description: Optional[str] = None
    images: Optional[list[str]] = None


@strawberry.input
class ReportListingInput:
    listing_id: int
    title: Optional[str] = None
    description: Optional[str] = None
    images: Optional[List[str]] = None


@strawberry.type
class ReportListingResponse:
    success: bool
    message: str


@strawberry.type
class ReportAdvertisementResponse:
    success: bool
    message: str


@strawberry.input
class ReportInput:
    entityId: int
    reason: Optional[List[str]] = None  # ✅ now supports an array of strings


@strawberry.type
class ResolveListingResponse:
    success: bool
    message: str


@strawberry.input
class ResolveAdvertisementInput:
    advertisement_id: int


@strawberry.input
class ResolveListingInput:
    listing_id: int


@strawberry.type
class ClientLocationType:
    id: int
    latitude: float
    longitude: float
    address: str
    city: str
    country: str
    postal_code: Optional[str]
    location_type: str
    created_at: datetime
    updated_at: datetime


@strawberry.type
class AdvertisementChannelReturnType:
    id: strawberry.ID
    channel_name: str
    sub_business_categories: list[str]
    city: Optional[str]
    province: Optional[str]
    locality: Optional[str]
    latitude: Optional[float]
    longitude: Optional[float]
    price_per_day: float
    apply_discount: bool
    discount_price: Optional[float]
    start_date: str
    end_date: Optional[str] = None
    status: str
    channel_image: Optional[str] = None  # New field for channel image URL

    @strawberry.field
    def business_categories(self) -> List[BusinessCategoryType]:
        return [
            BusinessCategoryType(
                id=cat.id,
                name=cat.name,
                created_at=cat.created_at,
                subcategories=[
                    BusinessSubCategoryType(id=sub.id, name=sub.name)
                    for sub in cat.subcategories.all()
                ]
            )
            for cat in self.business_categories.all()
        ]
#   @strawberry.field
#    def business_categories(self) -> List[BusinessCategoryType]:
#         return list(self.business_categories.all())

    @strawberry.field
    def advertisements(self) -> List[ChannelAdvertisementReturnType]:
        return [
            assignment.advertisement
            for assignment in self.advertisement_assignments.filter(
                advertisement__advertisement_status='active'
            )
            if assignment.advertisement 
            and not assignment.advertisement.is_flagged
            and assignment.advertisement.user
            and assignment.advertisement.user.is_active
            and assignment.advertisement.user.business_profile
            and not assignment.advertisement.user.business_profile.is_suspended
            and not assignment.advertisement.user.business_profile.is_deactivated
        ]

    @strawberry.field
    def business_profiles(self) -> List[BusinessUserProfileType]:
        # Collect unique business profiles from active and unflagged advertisements
        business_set = {
            assignment.advertisement.user.business_profile
            for assignment in self.advertisement_assignments.filter(
                advertisement__advertisement_status='active'
            )
            if (assignment.advertisement 
                and not assignment.advertisement.is_flagged
                and assignment.advertisement.user
                and assignment.advertisement.user.is_active
                and assignment.advertisement.user.business_profile
                and not assignment.advertisement.user.business_profile.is_suspended
                and not assignment.advertisement.user.business_profile.is_deactivated)
        }

        return [
            BusinessUserProfileType(
                business_name=bp.business_name,
                description=getattr(bp, "description", None),
                logo_url=getattr(bp, "logo_url", None),
                address=getattr(bp, "address", None),
                city=bp.city,
                state=bp.state,
                zip_code=bp.zip_code,
                latitude=getattr(bp, "latitude", None),
                longitude=getattr(bp, "longitude", None),
                phone=bp.phone,
                website=getattr(bp, "website", None),
                payment_methods=getattr(bp, "payment_methods", None),
                subcategories=getattr(bp, "subcategories", None),
                category_id=getattr(bp, "category_id", None),
                cover_image=getattr(bp, "cover_image", None),
            )
            for bp in business_set
        ]

    @strawberry.field
    def business_count(self) -> int:
        return len(self.business_profiles)


@strawberry.type
class BusinessProfileDetailsType:
    id: strawberry.ID
    business_uuid: Optional[uuid.UUID] = None
    business_name: str
    description: Optional[str]
    logo_url: Optional[str]
    hours: List[BusinessHoursType]
    location: LocationType
    contact: ContactInfoType
    payment_methods: List[str]
    category: Optional[str]
    subcategories: List[str]
    user: UserType
    advertisements: List[AdvertisementReturnType]
    listings: Optional[List[ListingType]] = None   # 👈 made optional
    cover_image: Optional[str] = None
    is_suspended: Optional[bool] = None


@strawberry.type
class AdvertisementWithChannlesReturnType:
    id: int
    advertisment_name: str
    advertisment_description: Optional[str]
    advertisement_image: List[str]
    start_date: date
    end_date: Optional[date]
    advertisment_cost: float
    advertisement_status: str
    is_flagged: bool
    is_ever_flagged: Optional[bool] = None
    flag: Optional[AdvertisementFlagType] = None
    business_profile: Optional[BusinessProfileReturnType] = None
    is_reported: Optional[bool] = None


@strawberry.type
class ChannelDetailsType:
    id: strawberry.ID
    channel_name: str
    city: Optional[str] = None
    province: Optional[str] = None
    locality: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    price_per_day: float
    apply_discount: bool
    start_date: str
    end_date: str
    status: str
    discount_price: float | None = None
    discount_status_message: Optional[str] = None
    channel_image: Optional[str] = None

    advertisements: Optional[List[AdvertisementWithChannlesReturnType]] = None
    business_profile: Optional[List[BusinessProfileReturnType]] = None
    categories: Optional[List[str]] = None
    discount_history: Optional[List[ChannelDiscountHistoryType]] = None


@strawberry.input
class SupportEmailInput:
    subject: str
    full_name: str
    designation: str
    message: str
    business_name: Optional[str] = None
    business_email: Optional[str] = None
    business_uuid: Optional[str] = None
    images: Optional[List[str]] = None   # optional array of image URLs


@strawberry.type
class SupportEmailResponse:
    success: bool
    message: str
    ticket_number: Optional[str] = None
@strawberry.type
class DefaultPaymentMethodResponse:
    success: bool
    message: str


@strawberry.type
class RequestPaymentOtpResponse:
    success: bool
    message: str


@strawberry.type
class VerifyPaymentOtpResponse:
    success: bool
    message: str
    payment_session_token: Optional[str] = None


@strawberry.type
class VerifySettingsOtpResponse:
    success: bool
    message: str
    expires_at: Optional[datetime] = None
    seconds_remaining: Optional[int] = None


@strawberry.type
class SettingsSessionStatusType:
    verified: bool
    expires_at: Optional[datetime] = None
    seconds_remaining: int = 0


@strawberry.type
class RequestPasswordOtpResponse:
    success: bool
    message: str


@strawberry.type
class VerifyPasswordOtpResponse:
    success: bool
    message: str
    password_session_token: Optional[str] = None


# ── Notification System Types ──────────────────────────────────────────

@strawberry.type
class NotificationType:
    id: strawberry.ID
    notification_type: str
    title: str
    message: str
    is_read: bool
    recipient_role: str
    advertisement_id: Optional[int] = None
    channel_id: Optional[int] = None
    invoice_id: Optional[int] = None
    created_at: datetime


@strawberry.type
class NotificationSuccessType:
    success: bool
    message: str


@strawberry.type
class NotificationCountType:
    count: int


# ── Business Deactivation Types ───────────────────────────────────────

@strawberry.type
class DeactivateBusinessResponse:
    success: bool
    message: str
