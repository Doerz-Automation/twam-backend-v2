from .models import BusinessSubCategory, ListingFlag, Listings, SupportTicket
import strawberry
import logging
import boto3
import uuid
from strawberry.types import Info
from api.types import (
    AdvertisementChannelType, AdvertisementInput, AdvertisementSuccessType, AdvertisementType, ApproveUserInput, BusinessHoursReturnType, BusinessHoursUpdateInput, BusinessSubCategoryType, ChangeAdvertisementStatusInput,
    ChangePasswordInput, ChannelType, ClientProfileType, CreateAdvertisementInput, CreateCategoryAndSubCategorySuccessType, CreateListingInput, LikedRespone, ListingSuccessType, LocationSaveRespone, RegisterClientInput, ReportListingInput, ReportListingResponse, ResolveListingInput, ResolveListingResponse, RevertSuspendedBusinessReturnType, SuccessAdvertisementCreatedType, SuccessEndListingType, SuccessUserApprovalType, SupportEmailInput, SupportEmailResponse, UnsaveAllBusinessProfilesResponse, UpdateAdvertisementChannelInput, UpdateAdvertisementInput, UpdateCategoryAndSubCategorySuccessType, UpdateClientProfileInput, UpdateListingInput,
    UserType, LoginResponse, ResendOTPResponse, ForgotPasswordResponse, ResetPasswordResponse, RegisterInput, LoginInput,
    AddCategoryInput, UpdateCategoryInput, BusinessProfileUpdateInput, BusinessProfileType, BusinessCategoryType,
    ReportAdvertisementInput, ReportAdvertisementResponse, ResolveAdvertisementInput, SuspensionType, CreateSuspensionInput, ReportInput, DefaultPaymentMethodResponse,
    RequestPaymentOtpResponse, VerifyPaymentOtpResponse, RequestPasswordOtpResponse, VerifyPasswordOtpResponse,
    NotificationSuccessType, DeactivateBusinessResponse, UpdateAccountHolderInput,
    VerifySettingsOtpResponse
)
from api.models import (AdvertisementReports, ListingReports,
                        Advertisement, AdvertisementCategory, AdvertisementChannel,
                        AdvertisementChannelAssignment, ChannelDiscountHistory, ChannelPriceHistory,
                        ClientLocation, ClientProfile, LikedAdvertisement,
                        LikedAdvertisementChannel, LikedBusinessProfile, User, Profile,
                        BusinessProfile, OTP, BusinessProfile, BusinessHours,
                        BusinessCategory, AdvertisementFlag, Listings, Suspension,
                        PaymentOtpAttempt, PasswordOtpAttempt, SettingsAccess
                        )
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from django.utils.crypto import get_random_string
from django.utils.timezone import now
from django.core.mail import send_mail
from django.contrib.auth import authenticate
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from api.utils import SESEmailSender, generate_jwt_token, generate_and_send_otp, generate_report_id
from api.stripe_services import create_stripe_customer, cancel_subscription, create_setup_intent, set_customer_default_payment_method
from api.decorators import (jwt_required, require_api_secret, require_authentication, require_role, require_not_suspended,
                            require_settings_verified, assert_settings_verified, get_access_token_jti)
from twam_api import settings
from graphql import GraphQLError
from django.contrib.auth import get_user_model
from strawberry.file_uploads import Upload
from typing import Optional, List

logger = logging.getLogger(__name__)
User = get_user_model()


def compute_master_clock_expiry(end_date):
    """Master Clock: compute expiry_at as 3:00 AM Toronto on the day after end_date.

    3 AM Toronto == midnight Vancouver, ensuring all Canadian timezones
    have crossed midnight before phase-out. Handles EDT/EST automatically.
    Returns a UTC datetime, or None if end_date is None.
    """
    if end_date is None:
        return None
    from zoneinfo import ZoneInfo
    from datetime import datetime, time, timezone as _utc, timedelta
    phase_out_date = end_date + timedelta(days=1)
    toronto_3am = datetime.combine(phase_out_date, time(3, 0, 0), tzinfo=ZoneInfo("America/Toronto"))
    return toronto_3am.astimezone(_utc.utc)


MAX_LISTING_IMAGES = 2


@strawberry.type
class Mutation:
    @strawberry.mutation
    @require_api_secret
    def register(self, info: Info, input: RegisterInput) -> UserType:
        first_name = input.first_name or ""
        last_name = input.last_name or ""

        user = User.objects.create_user(
            email=input.email.strip().lower(),
            password=input.password,
            first_name=first_name,
            last_name=last_name,
            role=input.role,
            is_active=False,
            is_verified=False,
            personal_phone=input.personal_phone or "",
        )

        if input.role == "user":
            Profile.objects.create(
                user=user,
                gender=input.gender,
                date_of_birth=input.date_of_birth,

            )
            user.is_admin_approved = True  # Automatically approve user
            user.save()
        elif input.role == "business":
            BusinessProfile.objects.create(
                user=user,
                business_name=input.business_name or "",
                address=input.business_address or "",
                city=input.business_city or "",
                state=input.business_province or "",
                zip_code=input.business_postal_code or "",
                logo_url=input.logo_url or "",
                phone=input.phone or "",
                description=input.business_description or "",
            )
        else:
            raise Exception("Invalid role provided.")

        # Generate OTP
        generate_and_send_otp(user, purpose="Signup")
        return UserType(
            id=user.id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            role=user.role,
            # Only user role will have this
            profile=getattr(user, "profile", None),
            is_active=user.is_active,
            is_verified=user.is_verified,
        )

    @strawberry.mutation
    @require_api_secret
    def resend_otp(self, info: Info, email: str) -> ResendOTPResponse:
        try:
            # Check if the user exists
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise GraphQLError("User with this email does not exist.")

        # Generate OTP and send it to the user's email
        message = generate_and_send_otp(user, purpose="Resend OTP")
        return ResendOTPResponse(success=True, message=message)

    @strawberry.mutation
    @require_api_secret
    def forgot_password(self, info: Info, email: str) -> ForgotPasswordResponse:
        try:
            user = User.objects.get(email=email.strip().lower())
            
            # Generate and send OTP for password reset
            generate_and_send_otp(user, purpose="Forgot Password")
            
            return ForgotPasswordResponse(
                success=True, 
                user_id=user.id, 
                message="OTP sent to email."
            )
        except User.DoesNotExist:
             # Security: don't reveal if user exists
            return ForgotPasswordResponse(
                success=True, 
                message="OTP sent to email."
            )

    @strawberry.mutation
    @require_authentication
    @jwt_required
    @require_api_secret
    @require_role(["business"])
    def create_setup_intent(self, info: Info) -> str:
        user = info.context.request.user
        return create_setup_intent(user)


    @strawberry.mutation
    @require_authentication
    @jwt_required
    @require_api_secret
    def reset_password(self, info: Info, user_id: int, new_password: str) -> ResetPasswordResponse:
        try:
            print(f"user_id: {user_id}")
            request = info.context.request
            user = request.user
            
            # Verify that the logged-in user matches the user_id
            if user.id != int(user_id):
                 raise GraphQLError("You are not authorized to reset this password.")

            # Set the new password
            user.set_password(new_password)
            user.save()
            
            return ResetPasswordResponse(success=True)

        except User.DoesNotExist:
            raise GraphQLError("User not found.")
        except Exception as e:
            raise GraphQLError(str(e))

    @strawberry.mutation
    @require_api_secret
    def trigger_channel_status_update(self, info: Info) -> str:
        """Manually trigger the channel/ad status update job"""
        from api.scheduler import update_channel_status
        import logging
        
        logger = logging.getLogger(__name__)
        
        try:
            logger.info("Manually triggering channel status update via GraphQL mutation")
            update_channel_status()
            return "Channel status update triggered successfully"
        except Exception as e:
            logger.error(f"Failed to trigger channel status update: {str(e)}", exc_info=True)
            raise GraphQLError(f"Error: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    def client_register(self, info: Info, input: RegisterClientInput) -> UserType:
        try:
            email = input.email.strip().lower()

            # Check if the email already exists
            if User.objects.filter(email=email).exists():
                raise GraphQLError(
                    "Email already exists. Please use a different email.")

            # Create a new user
            user = User.objects.create_user(
                email=email,
                first_name=input.first_name,
                last_name=input.last_name,
                role="user",
                is_active=True,
                is_verified=False,
                is_admin_approved=True

            )
            user.set_password(input.password)
            user.save()

            # Create client profile
            profile = ClientProfile.objects.create(
                user=user, date_of_birth=input.date_of_birth,)
            generate_and_send_otp(user, purpose="Signup")

            return UserType(
                id=user.id,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
                role=user.role,
                is_active=user.is_active,
                is_verified=user.is_verified,
                created_at=user.created_at,
                updated_at=user.updated_at,
                clientProfile=ClientProfileType(
                    id=profile.id,
                    gender=profile.gender,
                    profession=profile.profession,
                    interests=profile.interests,
                    created_at=profile.created_at,
                    updated_at=profile.updated_at,
                    selected_channels=[],
                    date_of_birth=profile.date_of_birth



                )
            )

        except Exception as e:
            raise GraphQLError(
                f"An error occurred during client registration: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    def verify_otp(self, info: Info, email: str, otp_code: str) -> LoginResponse:
        try:
            user = User.objects.get(email=email)
            otp_record = OTP.objects.filter(
                user=user, otp_code=otp_code).first()

            if not otp_record:
                raise GraphQLError("Invalid OTP.")
            if otp_record.is_expired():
                otp_record.delete()
                raise GraphQLError(
                    "OTP has expired. Please request a new one.")

            user.is_verified = True
            # Business users need is_active=True to log in after email
            # verification. Admin approval (is_admin_approved) controls
            # what they can do once logged in.
            if user.role == "business" and not user.is_active:
                user.is_active = True
            user.save()
            otp_record.delete()
            # Generate JWT tokens
            tokens = generate_jwt_token(user)

            # Return response object with user details and token
            return LoginResponse(
                id=user.id,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
                access_token=tokens["access"],
                refresh_token=tokens["refresh"],
                role=user.role,
                is_verified=user.is_verified,
                is_active=user.is_active,
                is_admin_approved=user.is_admin_approved,
            )
        except User.DoesNotExist:
            raise GraphQLError("User not found.")

    @strawberry.mutation
    @require_api_secret
    def say_hello(self, info: Info, name: str) -> str:

        return f"Hello, {name}!"

    @strawberry.mutation
    @require_api_secret
    def login(self, info: Info, input: LoginInput) -> LoginResponse:
        """
        Handles user login and returns a JWT token.
        """

        try:
            # Fetch user by email
            user = User.objects.get(email=input.email.strip().lower())

            print(user)

            # Check if user is verified
            if not user.is_verified:
                generate_and_send_otp(user, purpose="Signup")
                raise GraphQLError("User has not completed OTP verification.")

            # Check if business account has been deactivated
            if user.role == "business" and hasattr(user, 'business_profile') and user.business_profile.is_deactivated:
                raise GraphQLError(
                    "This account has been deactivated. Please register a new account.")

            # Check if user is active
            if not user.is_active:
                raise GraphQLError(
                    "User account is inactive. Please contact support.")

            # Verify the password
            if not user.check_password(input.password):
                raise Exception("Invalid email or password")

            # Generate JWT tokens
            tokens = generate_jwt_token(user)

            # Reset OTP verification attempts on successful login
            PaymentOtpAttempt.objects.filter(user=user).update(attempt_count=0, locked_at=None)
            PasswordOtpAttempt.objects.filter(user=user).update(attempt_count=0, locked_at=None)
            # A fresh login never inherits a previous Settings verification
            SettingsAccess.objects.filter(user=user).update(
                attempt_count=0, locked_at=None, verified_until=None, verified_jti="")

            # Return response object with user details and token
            return LoginResponse(
                id=user.id,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
                access_token=tokens["access"],
                refresh_token=tokens["refresh"],
                role=user.role,
                is_verified=user.is_verified,
                is_active=user.is_active,
                is_admin_approved=user.is_admin_approved,
            )
        except User.DoesNotExist:
            raise Exception("User with this email does not exist")

    @strawberry.mutation
    @require_api_secret
    def refresh_token(self, info: Info, refresh_token: str) -> str:
        try:
            from rest_framework_simplejwt.tokens import RefreshToken

            refresh = RefreshToken(refresh_token)
            return refresh.access_token
        except Exception as e:
            raise Exception("Invalid refresh token")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @require_role(["business"])
    def update_business_profile(
        self,
        info: Info,
        input: BusinessProfileUpdateInput
    ) -> BusinessProfileType:
        request = info.context["request"]
        user = request.user
        business_profile = user.business_profile

        # Onboarding (before admin approval) is not part of Settings. Once the
        # account is approved, every profile edit is a Settings edit and needs
        # an active Settings verification session.
        if user.is_admin_approved:
            assert_settings_verified(info)

        # Update basic fields
        if input.business_name is not None:
            business_profile.business_name = input.business_name
        if input.description is not None:
            business_profile.description = input.description

        if input.logo is not None:
            business_profile.logo_url = None if input.logo == "" else input.logo

        if input.cover_image is not None:
            business_profile.cover_image = None if input.cover_image == "" else input.cover_image

        # Update business hours
        if input.hours is not None:
            BusinessHours.objects.filter(business=business_profile).delete()
            for day_schedule in input.hours:
                if not day_schedule.day:
                    continue
                BusinessHours.objects.create(
                    business=business_profile,
                    day=day_schedule.day.strip().lower(),
                    opening_time=day_schedule.opening_time,
                    closing_time=day_schedule.closing_time,
                    is_closed=day_schedule.is_closed,
                    is_24hours=day_schedule.is_24hours or False,
                    is_overnight=day_schedule.is_overnight or False
                )

        # Location
        if input.location is not None:
            business_profile.address = input.location.address
            business_profile.address2 = input.location.address2
            business_profile.city = input.location.city
            business_profile.state = input.location.state
            business_profile.zip_code = input.location.zip_code
            business_profile.latitude = input.location.latitude
            business_profile.longitude = input.location.longitude
            if getattr(input.location, 'timezoneId', None):
                business_profile.timezone_id = input.location.timezoneId

        # Contact
        if input.contact is not None:
            business_profile.phone = input.contact.phone
            business_profile.website = input.contact.website

        # Payment methods
        if input.payment_methods is not None:
            business_profile.payment_methods = input.payment_methods

        # Category (ForeignKey)
        if input.category_id is not None:
            try:
                business_profile.category = BusinessCategory.objects.get(
                    id=input.category_id)
            except BusinessCategory.DoesNotExist:
                raise GraphQLError("Invalid business category ID.")

        # Subcategories
        if input.subcategories is not None:
            if len(set(input.subcategories)) > 3:
                raise GraphQLError(
                    "You can select a maximum of 3 subcategories.")
            business_profile.subcategories = input.subcategories

        business_profile.save()
        return BusinessProfileType.from_instance(business_profile)

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @require_role(["business"])
    @require_settings_verified
    def update_account_holder(self, info: Info, input: UpdateAccountHolderInput) -> UserType:
        request = info.context["request"]
        user = request.user
        user.first_name = input.first_name
        user.last_name = input.last_name
        user.save(update_fields=["first_name", "last_name"])
        return UserType(
            id=user.id,
            email=user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            role=user.role,
            is_active=user.is_active,
            is_admin_approved=user.is_admin_approved,
        )

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @require_role(["business"])
    def update_business_hours(self, info: Info, input: BusinessHoursUpdateInput) -> BusinessHoursReturnType:
        try:
            user = info.context.request.user
            business_profile = user.business_profile
            
            # Ensure the hour belongs to the user's business
            hour_record = BusinessHours.objects.get(id=input.id, business=business_profile)
            
            def update_field(obj, field_name, value):
                if value is not strawberry.UNSET:
                    setattr(obj, field_name, value)

            update_field(hour_record, "day", input.day)
            update_field(hour_record, "opening_time", input.opening_time)
            update_field(hour_record, "closing_time", input.closing_time)
            update_field(hour_record, "is_closed", input.is_closed)
            update_field(hour_record, "is_24hours", input.is_24hours)
            update_field(hour_record, "is_overnight", input.is_overnight)
            
            hour_record.save()
            
            return BusinessHoursReturnType(
                day=hour_record.day,
                opening_time=hour_record.opening_time.strftime("%H:%M:%S") if hour_record.opening_time else None,
                closing_time=hour_record.closing_time.strftime("%H:%M:%S") if hour_record.closing_time else None,
                is_closed=hour_record.is_closed,
                is_24hours=hour_record.is_24hours,
                is_overnight=hour_record.is_overnight
            )

        except BusinessHours.DoesNotExist:
             raise GraphQLError("Business Hour record not found or access denied.")
        except Exception as e:
             raise GraphQLError(f"Error updating business hours: {str(e)}")

    # @strawberry.mutation
    # def add_business_category(self, input: AddCategoryInput) -> BusinessCategoryType:
    #     try:
    #         category = BusinessCategory.objects.create(
    #             name=input.name, subcategory=input.subcategory)
    #         if input.subcategory is not None:
    #             category.subcategory = input.subcategory
    #         return category
    #     except Exception as e:
    #         raise Exception(f"Could not create category: {e}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    def add_business_category(self, info, input: AddCategoryInput) -> CreateCategoryAndSubCategorySuccessType:

        try:
            category = BusinessCategory.objects.create(name=input.cat)
            sub_objs = []
            for sub_name in input.sub:
                sub = BusinessSubCategory.objects.create(
                    category=category, name=sub_name)
                sub_objs.append(sub)
            print("category", category)
            return CreateCategoryAndSubCategorySuccessType(
                message="Category and subcategories created successfully", success="true"
            )
        except Exception as e:
            raise Exception(f"Could not update category: {e}")

    
    @strawberry.mutation
    @require_api_secret
    @require_authentication
    def update_category_and_subcat(self,info, input: UpdateCategoryInput)->UpdateCategoryAndSubCategorySuccessType:
        try:
            
            category = BusinessCategory.objects.get(pk=input.id)

                # ✅ Update category name if provided
            if input.cat is not None:
                    category.name = input.cat
                    category.save()

                # ✅ Update subcategories if provided
            if input.sub is not None:
                    # Simple strategy: replace all
                    category.subcategories.all().delete()

                    for sub_name in input.sub:
                        BusinessSubCategory.objects.create(
                            category=category,
                            name=sub_name
                        )

            return CreateCategoryAndSubCategorySuccessType(
                    message="Category and subcategories updated successfully",
                    success=True
                )

        except BusinessCategory.DoesNotExist:
            raise GraphQLError("Category not found")

        except Exception as e:
            raise GraphQLError(f"Could not update category: {str(e)}")
        
    @strawberry.mutation
    def update_business_category(self, input: UpdateCategoryInput) -> Optional[BusinessCategoryType]:
        try:
            category = BusinessCategory.objects.get(pk=input.id)
            category.name = input.name
            if input.subcategory is not None:
                category.subcategory = input.subcategory
            category.save()
            return category
        except BusinessCategory.DoesNotExist:
            return None
        except Exception as e:
            raise Exception(f"Could not update category: {e}")

    @strawberry.mutation
    def delete_business_category(self, id: strawberry.ID) -> bool:
        try:
            category = BusinessCategory.objects.get(pk=id)
            category.delete()
            return True
        except BusinessCategory.DoesNotExist:
            return False
        except Exception as e:
            raise Exception(f"Could not delete category: {e}")

    @strawberry.mutation
    @require_api_secret
    def create_admin(self, info: Info, first_name: str, last_name: str, email: str, password: str) -> UserType:
        try:
            user = User.objects.create_user(
                email=email,
                password=password,
                role="admin",
                first_name=first_name,
                last_name=last_name,
                is_active=True,
                is_verified=True
            )
            return UserType(
                id=user.id,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
                role=user.role,
                is_active=user.is_active,
                is_verified=user.is_verified,

            )
        except Exception as e:
            raise GraphQLError(f"Error creating admin: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def create_advertisemen_channel(self, info: Info, input: CreateAdvertisementInput) -> SuccessAdvertisementCreatedType:
        try:
            user = info.context.request.user
            if user.role != "admin":
                raise GraphQLError("Only admins can create advertisements.")

            # Master Clock: 3 AM Toronto on the day after end_date
            resolved_expiry_at = compute_master_clock_expiry(input.end_date)

            # Enforce next-day discount start
            from zoneinfo import ZoneInfo
            channel_tz = ZoneInfo(input.timezone or "America/Toronto")
            today_local = timezone.now().astimezone(channel_tz).date()

            if input.apply_discount:
                if input.start_date and input.end_date and input.start_date == input.end_date:
                    raise GraphQLError("Discounts cannot be applied to 1-day channels.")
                d_start = input.discount_start_date
                if not d_start or d_start <= today_local:
                    raise GraphQLError("Discount start date must be provided and be at least the next day.")

            # Validate discount date window falls within channel dates
            if input.apply_discount and (input.discount_start_date or input.discount_end_date):
                ch_start = input.start_date
                ch_end = input.end_date
                d_start = input.discount_start_date
                d_end = input.discount_end_date

                if d_start and d_start < ch_start:
                    raise GraphQLError("Discount start date cannot be before channel start date.")
                if ch_end and d_end and d_end > ch_end:
                    raise GraphQLError("Discount end date cannot be after channel end date.")
                if d_start and d_end and d_start > d_end:
                    raise GraphQLError("Discount start date must be before discount end date.")

            advertisement = AdvertisementChannel.objects.create(  # <-- updated here
                channel_name=input.channel_name,
                city=input.city,
                province=input.province,
                locality=input.locality,
                latitude=input.latitude,
                longitude=input.longitude,
                price_per_day=input.price_per_day,
                apply_discount=input.apply_discount,
                start_date=input.start_date,
                end_date=input.end_date,
                sub_business_categories=input.sub_business_categories or [],
                discount_price=input.discount_price,
                discount_start_date=input.discount_start_date,
                discount_end_date=input.discount_end_date,
                channel_image=input.channel_image,
                timezone=input.timezone or "America/Toronto",
                expiry_at=resolved_expiry_at,
            )

            # Seed initial discount history entries to accurately reflect the discount window.
            # If a windowed discount is set (start/end dates), we seed multiple history entries
            # so day-by-day billing picks up the right rate on every date.
            if input.apply_discount and input.discount_start_date:
                # Period 1: No discount from channel start → day before discount start
                if input.discount_start_date > input.start_date:
                    ChannelDiscountHistory.objects.create(
                        channel=advertisement,
                        apply_discount=False,
                        discount_price=0,
                        effective_from=input.start_date,
                        effective_until=input.discount_start_date,  # exclusive upper bound handled in query
                    )

                # Period 2: Discount active during the window
                ChannelDiscountHistory.objects.create(
                    channel=advertisement,
                    apply_discount=True,
                    discount_price=input.discount_price or 0,
                    effective_from=input.discount_start_date,
                    effective_until=input.discount_end_date,  # null if open-ended
                )

                # Period 3: If the discount has an end date, also seed a no-discount entry after it
                if input.discount_end_date:
                    from datetime import timedelta as _td
                    day_after_discount = input.discount_end_date + _td(days=1)
                    if input.end_date is None or day_after_discount <= input.end_date:
                        ChannelDiscountHistory.objects.create(
                            channel=advertisement,
                            apply_discount=False,
                            discount_price=0,
                            effective_from=day_after_discount,
                        )
            else:
                # Simple case: discount on from the very start (or no discount at all)
                ChannelDiscountHistory.objects.create(
                    channel=advertisement,
                    apply_discount=input.apply_discount,
                    discount_price=input.discount_price if input.apply_discount else 0,
                    effective_from=input.start_date,
                )

            # Seed initial price history so billing resolver can find the correct
            # base price for any day, even if price_per_day is updated later.
            ChannelPriceHistory.objects.create(
                channel=advertisement,
                price_per_day=input.price_per_day,
                effective_from=input.start_date,
                effective_until=None,
            )

            for category_id in input.business_category:
                try:
                    category = BusinessCategory.objects.get(id=category_id)
                    AdvertisementCategory.objects.create(
                        advertisement=advertisement, category=category)
                except BusinessCategory.DoesNotExist:
                    raise GraphQLError(f"Invalid category ID: {category_id}")

            return SuccessAdvertisementCreatedType(
                id=advertisement.id,
                message="Advertisement created successfully",
            )
        except Exception as e:
            raise GraphQLError(f"Error creating advertisement: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def update_advertisement_channel(self, info: Info, input: UpdateAdvertisementChannelInput) -> SuccessAdvertisementCreatedType:
        try:

            user = info.context.request.user
            if user.role != "admin":
                raise GraphQLError("Only admins can update advertisements.")

            ad = AdvertisementChannel.objects.get(id=input.id)
            original_end_date = ad.end_date  # Capture before any mutation

            # Reject start_date changes if the channel has already started —
            # retroactively changing it would corrupt billing records
            if input.start_date is not None and input.start_date != ad.start_date:
                if ad.start_date < date.today():
                    raise GraphQLError(
                        "Cannot change the start date of a channel that has already started. "
                        "This would retroactively affect billing."
                    )

            # --- Strict Locking Logic ---
            import zoneinfo
            from django.utils import timezone
            channel_tz = zoneinfo.ZoneInfo(input.timezone or ad.timezone or "America/Toronto")
            today_local = timezone.now().astimezone(channel_tz).date()

            # Enforce next-day discount start
            new_discount_start_date = getattr(input, 'discount_start_date', strawberry.UNSET)
            effective_apply_discount = input.apply_discount if input.apply_discount is not None else ad.apply_discount
            date_to_validate = new_discount_start_date if new_discount_start_date is not strawberry.UNSET else ad.discount_start_date

            effective_start = input.start_date if input.start_date is not None else ad.start_date
            effective_end = input.end_date if input.end_date is not None else ad.end_date

            if effective_apply_discount:
                if effective_start and effective_end and effective_start == effective_end:
                    raise GraphQLError("Discounts cannot be applied to 1-day channels.")
                if not date_to_validate or date_to_validate <= today_local:
                    # allow already-active discounts to keep their current start date to allow maintenance updates
                    if not (ad.apply_discount and ad.discount_start_date == date_to_validate):
                        raise GraphQLError("Discount start date must be provided and be at least the next day.")

            # Rule: Lock discount fields ONLY if the discount is:
            #   a) Currently active (discount_start_date <= today <= discount_end_date or no end date), OR
            #   b) Imminent — starts tomorrow (discount_start_date == today + 1 day)
            # Discounts scheduled more than 1 day out are still editable.
            from datetime import timedelta
            discount_start = ad.discount_start_date
            discount_end = ad.discount_end_date
            tomorrow_local = today_local + timedelta(days=1)

            discount_is_active = (
                ad.apply_discount and
                discount_start is not None and
                discount_start <= today_local and
                (discount_end is None or discount_end >= today_local)
            )
            discount_is_imminent = (
                ad.apply_discount and
                discount_start is not None and
                discount_start == tomorrow_local
            )
            is_locked = discount_is_active or discount_is_imminent

            if is_locked:
                locked_fields_changed = False
                
                # Check for attempts to modify locked fields
                if input.apply_discount is not None and input.apply_discount != ad.apply_discount:
                    locked_fields_changed = True
                if input.discount_price is not None and getattr(input, 'discount_price', None) != ad.discount_price:
                    locked_fields_changed = True
                if input.start_date is not None and input.start_date != ad.start_date:
                    locked_fields_changed = True
                if input.end_date is not strawberry.UNSET and input.end_date != ad.end_date:
                    locked_fields_changed = True
                if getattr(input, 'discount_start_date', strawberry.UNSET) is not strawberry.UNSET and getattr(input, 'discount_start_date', None) != ad.discount_start_date:
                    locked_fields_changed = True
                if getattr(input, 'discount_end_date', strawberry.UNSET) is not strawberry.UNSET and getattr(input, 'discount_end_date', None) != ad.discount_end_date:
                    locked_fields_changed = True

                if locked_fields_changed:
                    raise GraphQLError(
                        "Cannot modify discount settings while the discount is active or starting tomorrow. "
                        f"The discount is locked until it has been active for at least one more day."
                    )

            # Determine if the discount settings are changing
            new_apply_discount = input.apply_discount if input.apply_discount is not None else ad.apply_discount
            new_discount_price = input.discount_price if input.discount_price is not None else ad.discount_price
            
            # Using getattr to safely fetch time fields, since they are newly added
            new_discount_start_date = getattr(input, 'discount_start_date', strawberry.UNSET)
            new_discount_end_date = getattr(input, 'discount_end_date', strawberry.UNSET)
            
            # Resolve the new final values for start and end times to compare thoroughly
            resolved_start_date = new_discount_start_date if new_discount_start_date is not strawberry.UNSET else ad.discount_start_date
            resolved_end_date = new_discount_end_date if new_discount_end_date is not strawberry.UNSET else ad.discount_end_date

            discount_changing = (
                new_apply_discount != ad.apply_discount or
                (new_apply_discount and new_discount_price != ad.discount_price) or
                (new_apply_discount and resolved_start_date != ad.discount_start_date) or
                (new_apply_discount and resolved_end_date != ad.discount_end_date)
            )

            # Skip discount and price fields in the setattr loop — we handle them separately
            DEFERRED_FIELDS = {"apply_discount", "discount_price", "discount_start_date", "discount_end_date", "price_per_day"}

            for field in vars(input):
                if field not in ["id", "business_category"] and field not in DEFERRED_FIELDS:
                    value = getattr(input, field)
                    if field in ["end_date", "expiry_at"]:
                        if value is not strawberry.UNSET:
                            setattr(ad, field, value)
                    elif value is not None:
                        # Skip if UNSET object from strawberry gets sneaked in to update optional values improperly
                        if value is not strawberry.UNSET:
                            setattr(ad, field, value)

            # Master Clock: recalculate expiry_at if end_date changed
            if input.end_date is not strawberry.UNSET and input.expiry_at is strawberry.UNSET:
                ad.expiry_at = compute_master_clock_expiry(ad.end_date)

            if discount_changing:
                if is_locked:
                    # Discount is active or starting tomorrow — defer changes safely via pending fields.
                    # The midnight scheduler will promote these without disrupting current billing.
                    ad.pending_apply_discount = new_apply_discount
                    ad.pending_discount_price = new_discount_price if new_apply_discount else 0
                    
                    if new_discount_start_date is not strawberry.UNSET:
                        ad.pending_discount_start_date = new_discount_start_date
                    if new_discount_end_date is not strawberry.UNSET:
                        ad.pending_discount_end_date = new_discount_end_date
                    
                    from zoneinfo import ZoneInfo
                    toronto_3am = datetime.combine(tomorrow_local, time(3, 0, 0), tzinfo=ZoneInfo("America/Toronto"))
                    ad.pending_discount_effective_at = toronto_3am.astimezone(dt_timezone.utc)
                    
                    discount_msg = f" Discount change will take effect at 3:00 AM Toronto ({ad.pending_discount_effective_at.strftime('%Y-%m-%d %H:%M:%S UTC')})."

                    # Notify: discount added/changed
                    from api.notification_service import create_notification
                    from api.models import AdvertisementChannelAssignment
                    assignments = AdvertisementChannelAssignment.objects.filter(
                        advertisement_channel=ad
                    ).select_related("advertisement__user")
                    
                    from api.models import BusinessProfile, User
                    from django.db.models import Q
                    
                    channel_cat_ids = ad.advertisement_category.values_list('category_id', flat=True)
                    interested_biz_profiles = BusinessProfile.objects.filter(category_id__in=channel_cat_ids)
                    
                    if ad.province:
                        interested_biz_profiles = interested_biz_profiles.filter(
                            Q(state__iexact=ad.province) | Q(state__isnull=True) | Q(state="")
                        )
                    
                    interested_user_ids = interested_biz_profiles.values_list('user_id', flat=True)
                    assigned_user_ids = assignments.values_list('advertisement__user_id', flat=True)
                    all_target_user_ids = set(list(interested_user_ids) + list(assigned_user_ids))
                    all_target_users = User.objects.filter(id__in=all_target_user_ids)

                    for biz_user in all_target_users:
                        if new_apply_discount:
                            create_notification(
                                recipient=biz_user,
                                notification_type="discount_started",
                                title=f'Special Discount on "{ad.channel_name}"',
                                message=f'A discount of ${new_discount_price} has been scheduled on channel "{ad.channel_name}". It will take effect tomorrow. Join now to save!',
                                channel_id=ad.id,
                                recipient_role="business",
                            )
                        else:
                            create_notification(
                                recipient=biz_user,
                                notification_type="discount_ended",
                                title=f'Discount removed from "{ad.channel_name}"',
                                message=f'The discount on channel "{ad.channel_name}" has been scheduled for removal. The change takes effect tomorrow.',
                                channel_id=ad.id,
                                recipient_role="business",
                            )

                    if new_apply_discount:
                        from api.models import LikedAdvertisementChannel
                        from api.notification_service import create_bulk_notifications
                        
                        likes = LikedAdvertisementChannel.objects.filter(
                            advertisement_channel=ad
                        ).select_related("client_profile__user")
                        
                        client_users = [like.client_profile.user for like in likes]
                        if client_users:
                            create_bulk_notifications(
                                recipients=client_users,
                                notification_type="discount_available",
                                title="Special Discount on your search!",
                                message=f'A new discount of ${new_discount_price} has been scheduled for "{ad.channel_name}"! It will be live starting tomorrow.',
                                recipient_role="user",
                                channel_id=ad.id,
                            )

                else:
                    # Discount is in the future (starts > 1 day from now) — apply changes directly
                    # to live fields so channel details reflects the change immediately.
                    ad.apply_discount = new_apply_discount
                    ad.discount_price = new_discount_price if new_apply_discount else 0
                    ad.discount_start_date = resolved_start_date
                    ad.discount_end_date = resolved_end_date

                    # Clear any stale pending fields that might have been set previously
                    ad.pending_apply_discount = None
                    ad.pending_discount_price = None
                    ad.pending_discount_start_date = None
                    ad.pending_discount_end_date = None
                    ad.pending_discount_effective_at = None

                    # Rebuild ChannelDiscountHistory from today onwards to reflect new settings.
                    # Delete all future / open history entries, then re-seed.
                    from api.models import ChannelDiscountHistory
                    ChannelDiscountHistory.objects.filter(
                        channel=ad,
                        effective_from__gt=today_local
                    ).delete()

                    # Close any currently-open (no end) entry at today
                    open_entry = ChannelDiscountHistory.objects.filter(
                        channel=ad,
                        effective_until__isnull=True
                    ).first()
                    if open_entry:
                        open_entry.effective_until = today_local
                        open_entry.save(update_fields=["effective_until"])

                    # Re-seed based on the new discount settings
                    if new_apply_discount and resolved_start_date:
                        # Gap period: no discount from today → day before discount starts
                        if resolved_start_date > today_local:
                            ChannelDiscountHistory.objects.create(
                                channel=ad,
                                apply_discount=False,
                                discount_price=0,
                                effective_from=today_local + timedelta(days=1),
                                effective_until=resolved_start_date,
                            )
                        # Discount window
                        ChannelDiscountHistory.objects.create(
                            channel=ad,
                            apply_discount=True,
                            discount_price=new_discount_price or 0,
                            effective_from=max(today_local + timedelta(days=1), resolved_start_date),
                            effective_until=resolved_end_date,
                        )
                        # Post-window: seed no-discount entry after discount ends
                        if resolved_end_date:
                            day_after = resolved_end_date + timedelta(days=1)
                            if ad.end_date is None or day_after <= ad.end_date:
                                ChannelDiscountHistory.objects.create(
                                    channel=ad,
                                    apply_discount=False,
                                    discount_price=0,
                                    effective_from=day_after,
                                )
                    else:
                        # No discount — seed a single no-discount entry from today
                        ChannelDiscountHistory.objects.create(
                            channel=ad,
                            apply_discount=False,
                            discount_price=0,
                            effective_from=today_local + timedelta(days=1),
                        )

                    discount_msg = ""
            else:
                discount_msg = ""

            # --- Cascade shortened end_date to affected Ads ---
            # If admin moved the end_date earlier, find all ads on this channel
            # whose end_date is now beyond the new channel end_date and shorten
            # them — BUT only down to the latest end_date of their OTHER linked
            # channels. An ad linked to Channel 1 (now ending Mar 10) AND
            # Channel 2 (ending Mar 26) should only be shortened to Mar 26,
            # not Mar 10, because Channel 2 is still alive.
            cascaded_ads = 0
            new_end_date = getattr(input, "end_date", strawberry.UNSET)
            if new_end_date is not None and new_end_date is not strawberry.UNSET and (original_end_date is None or new_end_date < original_end_date):
                from api.models import AdvertisementChannelAssignment
                from django.db.models import Max

                affected_assignments = AdvertisementChannelAssignment.objects.filter(
                    advertisement_channel=ad,
                    advertisement__advertisement_status="active",
                    advertisement__end_date__gt=new_end_date,
                )
                for assignment in affected_assignments:
                    affected_ad = assignment.advertisement

                    # Check if any OTHER linked channels are ongoing (no end_date)
                    other_channels = affected_ad.channel_assignments.exclude(
                        advertisement_channel=ad
                    )
                    has_ongoing_other = other_channels.filter(
                        advertisement_channel__end_date__isnull=True,
                        advertisement_channel__status="active",
                    ).exists()

                    if has_ongoing_other:
                        # A sibling channel has no end_date — ad is not constrained.
                        logger.info(
                            f'Ad "{affected_ad.advertisment_name}" (ID: {affected_ad.id}) '
                            f'skipped cascade — still linked to an ongoing channel.'
                        )
                        continue

                    # Find the furthest end_date among other active sibling channels
                    other_max_end = other_channels.filter(
                        advertisement_channel__status="active",
                    ).aggregate(
                        max_end=Max("advertisement_channel__end_date")
                    )["max_end"]

                    # Only shorten to the later of: new channel end or sibling max end
                    safe_end = (
                        other_max_end
                        if other_max_end is not None and other_max_end > new_end_date
                        else new_end_date
                    )

                    if affected_ad.end_date > safe_end:
                        affected_ad.end_date = safe_end
                        affected_ad.save(update_fields=["end_date", "updated_at"])
                        cascaded_ads += 1
                        logger.info(
                            f'Ad "{affected_ad.advertisment_name}" (ID: {affected_ad.id}) '
                            f'end_date shortened to {safe_end} '
                            f'(channel updated to {new_end_date}, sibling channels max end: {other_max_end}).'
                        )

            # Handle price_per_day change: close current price history entry and open a new one
            new_price_per_day = getattr(input, 'price_per_day', None)
            if new_price_per_day is not None and new_price_per_day != ad.price_per_day:
                # Close the currently open price history entry (effective_until = today)
                open_price_entry = ChannelPriceHistory.objects.filter(
                    channel=ad,
                    effective_until__isnull=True
                ).first()
                if open_price_entry:
                    open_price_entry.effective_until = today_local
                    open_price_entry.save(update_fields=["effective_until"])

                # Open a new entry effective from tomorrow
                ChannelPriceHistory.objects.create(
                    channel=ad,
                    price_per_day=new_price_per_day,
                    effective_from=today_local + timedelta(days=1),
                    effective_until=None,
                )
                ad.price_per_day = new_price_per_day

            ad.save()

            if input.business_category is not None:
                ad.advertisement_category.all().delete()
                for category_id in input.business_category:
                    category = BusinessCategory.objects.get(id=category_id)
                    AdvertisementCategory.objects.create(
                        advertisement=ad, category=category)

            cascade_msg = f" {cascaded_ads} ad(s) were shortened to match." if cascaded_ads else ""
            return SuccessAdvertisementCreatedType(
                id=ad.id,
                message=f"Channel updated successfully.{discount_msg}{cascade_msg}"
            )

        except AdvertisementChannel.DoesNotExist:
            raise GraphQLError("Advertisement not found.")
        except BusinessCategory.DoesNotExist:
            raise GraphQLError("One or more BusinessCategory IDs are invalid.")
        except Exception as e:
            raise GraphQLError(f"Error updating advertisement: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def change_advertisement_channel_status(
        self, info: Info, input: ChangeAdvertisementStatusInput
    ) -> SuccessAdvertisementCreatedType:
        try:
            user = info.context.request.user

            if user.role != "admin":
                raise GraphQLError(
                    "Only admins can change advertisement status.")

            ad = AdvertisementChannel.objects.get(
                id=input.advertisement_id)  # <-- updated here
            ad.status = input.status
            ad.save()

            return SuccessAdvertisementCreatedType(
                id=ad.id,
                message=f"Advertisement status changed to {input.status} successfully."
            )

        except AdvertisementChannel.DoesNotExist:  # <-- updated here
            raise GraphQLError("Advertisement not found.")
        except Exception as e:
            raise GraphQLError(f"Error updating status: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def approve_user(self, info: Info, input: ApproveUserInput) -> SuccessUserApprovalType:
        try:
            admin = info.context.request.user

            if admin.role != "admin":
                raise GraphQLError("Only admins can approve users.")

            user_to_approve = User.objects.get(id=input.user_id)

            user_to_approve.is_admin_approved = True
            user_to_approve.is_active = True
            user_to_approve.updated_at = now()
            user_to_approve.save()
            
            # Notify business about approval — two separate notifications,
            # per client requirement (not combined into one).
            from api.notification_service import create_notification
            create_notification(
                recipient=user_to_approve,
                notification_type="business_approved",
                title="Account Approved",
                message="Congratulations! Your TWAM account has been approved.",
            )
            create_notification(
                recipient=user_to_approve,
                notification_type="business_approved_billing_reminder",
                title="Account Approved",
                message=(
                    "You can now start using Free Daily Ad section right away. "
                    "To start using our Ad Channels, please update your billing "
                    "information first. Also, please note any and all promotional "
                    "periods of our Ad Channels will be billed accordingly. To "
                    "update the billing info please go to Settings > Billing > "
                    "Invoices & Payments > Update Card."
                ),
            )

            return SuccessUserApprovalType(
                id=user_to_approve.id,
                message=f"User {user_to_approve.email} has been approved."
            )

        except User.DoesNotExist:
            raise GraphQLError("User not found.")
        except Exception as e:
            raise GraphQLError(f"Error approving user: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def instigate_user(self, info: Info, input: ApproveUserInput) -> SuccessUserApprovalType:
        try:
            admin = info.context.request.user

            if admin.role != "admin":
                raise GraphQLError("Only admins can approve users.")

            user_to_approve = User.objects.get(id=input.user_id)

            user_to_approve.is_admin_approved = None
            user_to_approve.updated_at = now()
            user_to_approve.save()

            return SuccessUserApprovalType(
                id=user_to_approve.id,
                message=f"User {user_to_approve.email} is instigated."
            )

        except User.DoesNotExist:
            raise GraphQLError("User not found.")
        except Exception as e:
            raise GraphQLError(f"Error approving user: {str(e)}")

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    @require_settings_verified
    def change_password(self, info: Info, input: ChangePasswordInput) -> str:
        request = info.context.request
        user = request.user  # Get the currently logged-in user

        if not user.check_password(input.current_password):
            raise Exception("Current password is incorrect")

        user.set_password(input.new_password)
        user.save()

        return "Password updated successfully"

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    @require_settings_verified
    def change_email(self, info: Info, email: str) -> str:
        try:
            new_email = email.strip().lower()
            request = info.context.request
            user = request.user
            # Check if the user is authenticated
            if not user.is_authenticated:
                raise GraphQLError("User is not authenticated.")
            if User.objects.filter(email=new_email).exclude(id=user.id).exists():
                raise GraphQLError("Email already exists.")
            if user.email == new_email:
                raise GraphQLError(
                    "The new email is the same as the current email")
            generate_and_send_otp(user, new_email=new_email)

            return "Email change request sent. Please verify your new email address."
        except Exception as e:
            raise GraphQLError(f"An error occurred: {str(e)}")

    @strawberry.field
    @require_authentication
    @jwt_required
    @require_api_secret
    @require_settings_verified
    def verifyOtpForNewEmail(self, info, otp_code: str, new_email: str) -> str:
        try:
            user = info.context.request.user

            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated first")

            new_email = new_email.strip().lower()
            otp = OTP.objects.filter(user=user, otp_code=otp_code).first()

            if not otp:
                raise GraphQLError("Invalid OTP code.")

            # Check if OTP has expired
            if otp.is_expired():
                otp.delete()
                raise GraphQLError("OTP has expired")

            # Update user model
            user.email = new_email
            user.save()

            otp.delete()

            return "Email updated successfully. Please log in with your new email address."

        except Exception as e:
            raise GraphQLError(f"An error occurred: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_not_suspended
    def create_advertisement(self, info, input: AdvertisementInput) -> AdvertisementSuccessType:
        user = info.context.request.user
        if user.role != "business":
            raise GraphQLError(
                "Only users with role 'business' can create advertisements.")

        try:
            # --- Reuse Constraints ---
            if input.reused_from_id:
                try:
                    source_ad = Advertisement.objects.get(id=input.reused_from_id)
                except Advertisement.DoesNotExist:
                    raise GraphQLError("The advertisement you are trying to reuse does not exist.")

                # Status Lock: cannot reuse an advertisement that is still active
                if source_ad.advertisement_status == "active":
                    raise GraphQLError(
                        "Cannot reuse an active advertisement. "
                        "The original ad must be inactive (e.g. cancelled or expired) before it can be reused."
                    )

                # One-Time Limit: each ad can only be the source for one new ad
                if source_ad.reused_to.exists():
                    raise GraphQLError(
                        "This advertisement has already been reused. "
                        "An advertisement can only be used as a source once."
                    )

            # --- Image Validation ---
            if not input.advertisement_image or len(input.advertisement_image) == 0:
                raise GraphQLError("At least one advertisement image is required.")

            # 1. Create or get Stripe Customer
            stripe_customer_id = create_stripe_customer(user)
            user.save() # Ensure user updates if we stored customer_id on user model (optional, currently not on User model but Advertisement)

            # 2. Calculate end_date from duration if provided
            end_date = input.end_date
            if input.duration_in_weeks:
                end_date = input.start_date + timedelta(weeks=input.duration_in_weeks)

            # Master Clock: 3 AM Toronto on the day after end_date
            resolved_expiry_at = compute_master_clock_expiry(end_date)

            # --- Validate channels and check all constraints BEFORE writing anything ---
            channels = []
            max_channel_expiry = None

            for channel_id in input.channel_ids:
                try:
                    channel = AdvertisementChannel.objects.get(id=channel_id)
                    channels.append(channel)
                    if channel.expiry_at:
                        if max_channel_expiry is None or channel.expiry_at > max_channel_expiry:
                            max_channel_expiry = channel.expiry_at
                except AdvertisementChannel.DoesNotExist:
                    raise GraphQLError(f"Invalid channel ID: {channel_id}")

            if resolved_expiry_at and max_channel_expiry:
                if resolved_expiry_at > max_channel_expiry:
                    latest_channel = max(channels, key=lambda c: c.expiry_at if c.expiry_at else datetime.min.replace(tzinfo=dt_timezone.utc))
                    raise GraphQLError(
                        f"Your selected end date exceeds the maximum lifespan of your latest selected channel '{latest_channel.channel_name}'. "
                        f"The latest channel closes at {latest_channel.expiry_at} UTC."
                    )

            for channel in channels:
                conflict = AdvertisementChannelAssignment.objects.filter(
                    advertisement_channel=channel,
                    advertisement__user=user,
                    advertisement__advertisement_status__in=['active', 'pending'],
                    advertisement__start_date__lte=end_date or date(9999, 12, 31),
                ).exclude(
                    advertisement__end_date__lt=input.start_date
                )
                if conflict.exists():
                    raise GraphQLError(
                        f'You already have an active ad on channel "{channel.channel_name}". '
                        f'Only one ad per channel is allowed at a time.'
                    )

            # --- All checks passed — write ad + assignments atomically ---
            with transaction.atomic():
                ad = Advertisement.objects.create(
                    user=user,
                    advertisment_name=input.advertisment_name,
                    advertisment_description=input.advertisment_description,
                    advertisement_image=input.advertisement_image,
                    start_date=input.start_date,
                    end_date=end_date,
                    expiry_at=resolved_expiry_at,
                    timezone=input.timezone or "America/Toronto",
                    advertisment_cost=input.advertisment_cost,
                    advertisement_status=input.advertisement_status or "active",
                    stripe_subscription_id=None,
                    stripe_customer_id=stripe_customer_id,
                    billing_status="pending",
                    last_billed_at=None,
                    reused_from_id=input.reused_from_id
                )
                for channel in channels:
                    AdvertisementChannelAssignment.objects.create(
                        advertisement=ad, advertisement_channel=channel)

            return AdvertisementSuccessType(
                id=ad.id,
                message="Advertisement created successfully. Charges will be accrued monthly.",
                clientSecret=None
            )
        except GraphQLError:
            raise
        except Exception as e:
            raise GraphQLError(f"Error creating advertisement: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def cancel_advertisement(self, info, advertisement_id: int) -> AdvertisementSuccessType:
        user = info.context.request.user
        try:
            ad = Advertisement.objects.get(id=advertisement_id, user=user)
            
            # Cancel advertisement logic for monthly post-paid model
            # Just mark as canceled. Charges will be finalized at end of month.
            ad.advertisement_status = "canceled"
            ad.billing_status = "canceled" # Or keep as is depending on billing rules
            ad.save()
            return AdvertisementSuccessType(id=ad.id, message="Advertisement cancelled successfully")

        except Advertisement.DoesNotExist:
             raise GraphQLError("Advertisement not found or you do not have permission to cancel it.")
        except Exception as e:
            raise GraphQLError(f"Error cancelling advertisement: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_not_suspended
    def create_listings(self, info, input: CreateListingInput) -> ListingSuccessType:
        user = info.context.request.user
        if user.role != "business":
            raise GraphQLError(
                "Only users with role 'business' can create listings")

        if input.listing_image and len(input.listing_image) > MAX_LISTING_IMAGES:
            raise GraphQLError(
                f"A listing can have at most {MAX_LISTING_IMAGES} photos.")

        try:
            listing = Listings.objects.create(
                user=user,
                listing_name=input.listing_name,
                listing_description=input.listing_description or "",
                listing_image=input.listing_image or [],
                listing_status=input.listing_status if input.listing_status else "active"
            )

            return ListingSuccessType(id=listing.id, message="Listing created successfully")
        except Exception as e:
            raise GraphQLError(f"Error creating Listing: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_not_suspended
    def update_listing(self, info, input: UpdateListingInput) -> ListingSuccessType:
        user = info.context.request.user

        if user.role != "business":
            raise GraphQLError(
                "Only users with role 'business' can update listings")

        if input.listing_image is not None and len(input.listing_image) > MAX_LISTING_IMAGES:
            raise GraphQLError(
                f"A listing can have at most {MAX_LISTING_IMAGES} photos.")

        try:

            listing = Listings.objects.get(id=input.id, user=user)
            updatable_fields = [
                "listing_name",
                "listing_description",
                "listing_image",
                "listing_status",
            ]

            for field in updatable_fields:
                value = getattr(input, field, None)
                if value is not None:
                    setattr(listing, field, value)
            listing.admin_actions = "resolved"
            listing.is_reported = False
            listing.save(update_fields=["admin_actions",
                                        "is_reported"])
            listing.save()
            if hasattr(listing, "flag"):
                listing.flag.delete()
                
            return ListingSuccessType(
                id=listing.id,
                message="Listing updated successfully"
            )
        except Listings.DoesNotExist:
            raise GraphQLError(
                "Listing not found or you don't have permission")
        except Exception as e:
            raise GraphQLError(f"Error updating Listing: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_not_suspended
    def update_advertisement(self, info: Info, input: UpdateAdvertisementInput) -> AdvertisementSuccessType:
        user = info.context.request.user

        if not hasattr(user, 'role') or user.role != "business":
            raise GraphQLError(
                "Only users with role 'business' can update advertisements.")

        try:
            ad = Advertisement.objects.get(id=input.id, user=user)

            # Reject start_date changes if the ad has already started —
            # retroactively changing it would corrupt billing records
            if input.start_date is not None:
                today = date.today()
                if ad.start_date < today:
                    raise GraphQLError(
                        "Cannot change the start date of an advertisement that has already started. "
                        "This would retroactively affect billing."
                    )

            # --- Image Validation ---
            if input.advertisement_image is not None and len(input.advertisement_image) == 0:
                raise GraphQLError("At least one advertisement image is required.")

            # Fields to update dynamically
            updatable_fields = [
                "advertisment_name",
                "advertisment_description",
                "advertisement_image",
                "start_date",
                "end_date",
                "expiry_at",
                "timezone",
                "advertisment_cost",
                "advertisement_status"
            ]

            for field in updatable_fields:
                value = getattr(input, field, None)
                if value is not None:
                    setattr(ad, field, value)

            # Master Clock: recalculate expiry_at if end_date changed
            if input.end_date is not None:
                ad.expiry_at = compute_master_clock_expiry(ad.end_date)

            ad.save()

            if hasattr(ad, "flag"):
                ad.flag.delete()

            # Update channel assignments if provided
            if input.channel_ids is not None:
                ad.channel_assignments.all().delete()
                channels = AdvertisementChannel.objects.filter(
                    id__in=input.channel_ids)
                if len(channels) != len(input.channel_ids):
                    raise GraphQLError("One or more channel IDs are invalid.")
                    
                max_channel_expiry = None
                for channel in channels:
                    if channel.expiry_at:
                        if max_channel_expiry is None or channel.expiry_at > max_channel_expiry:
                            max_channel_expiry = channel.expiry_at
                            
                if ad.expiry_at and max_channel_expiry:
                    if ad.expiry_at > max_channel_expiry:
                        from datetime import datetime, timezone as dt_timezone
                        latest_channel = max(channels, key=lambda c: c.expiry_at if c.expiry_at else datetime.min.replace(tzinfo=dt_timezone.utc))
                        raise GraphQLError(
                            f"Your selected end date exceeds the maximum lifespan of your latest selected channel '{latest_channel.channel_name}'. "
                            f"The latest channel closes at {latest_channel.expiry_at} UTC."
                        )

                for channel in channels:
                    conflict = AdvertisementChannelAssignment.objects.filter(
                        advertisement_channel=channel,
                        advertisement__user=ad.user,
                        advertisement__advertisement_status__in=['active', 'pending'],
                        advertisement__start_date__lte=ad.end_date or date(9999, 12, 31),
                    ).exclude(
                        advertisement__end_date__lt=ad.start_date
                    ).exclude(
                        advertisement=ad
                    )
                    if conflict.exists():
                        raise GraphQLError(
                            f'Another active ad already exists on channel "{channel.channel_name}". '
                            f'Only one ad per channel is allowed at a time.'
                        )

                AdvertisementChannelAssignment.objects.bulk_create([
                    AdvertisementChannelAssignment(
                        advertisement=ad, advertisement_channel=channel)
                    for channel in channels
                ])
            ad.admin_actions = "resolved"
            ad.is_reported = False
            ad.save(update_fields=["admin_actions",
                                   "is_reported"])
            return AdvertisementSuccessType(id=ad.id, message="Advertisement updated successfully")

        except Advertisement.DoesNotExist:
            raise GraphQLError(
                "Advertisement not found or you don't have permission to update it.")
        except Exception as e:
            raise GraphQLError(f"Error updating advertisement: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def change_advertisement_status(self, info: Info, input: ChangeAdvertisementStatusInput) -> AdvertisementSuccessType:
        user = info.context.request.user

        if not hasattr(user, 'role') or (user.role != "business" and user.role != "admin"):
            raise GraphQLError(
                "Only users with role 'business' or 'admin' can update advertisements status.")
        try:
            ad = Advertisement.objects.get(id=input.id)
            print(f"DEBUG: Changing ad {ad.id} status to {input.status}. Sub ID: {ad.stripe_subscription_id}")
            
            # If status is being changed to 'canceled' or 'phaseout', handle Stripe cancellation
            # If status is being changed to 'canceled' or 'phaseout', we just update local status
            # Post-paid monthly billing does not require immediate refund logic.
            # Charges are aggregated at month end based on active days/spend.
                
            ad.advertisement_status = input.status
            ad.save()

            return AdvertisementSuccessType(id=ad.id, message=f"Advertisement status changed to {input.status}")
        except Advertisement.DoesNotExist:
            raise GraphQLError("Advertisement not found.")
        except Exception as e:
            raise GraphQLError(f"Error changing status: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def report_advertisement(self, info, input: ReportAdvertisementInput) -> ReportAdvertisementResponse:
        request = info.context.request
        user = request.user

        try:
            ad = Advertisement.objects.get(pk=input.advertisement_id)
        except Advertisement.DoesNotExist:
            return ReportAdvertisementResponse(success=False, message="Advertisement not found.")

        if ad.active_flag:
            return ReportAdvertisementResponse(success=False, message="This advertisement has already been reported.")
        
        ad.is_ever_flagged = True  # Mark as flagged
        ad.save() 

        try:
             AdvertisementFlag.objects.create(
                advertisement=ad,
                flagged_by=user,
                title=input.title or "",
                description=input.description or "",
                images=input.images or [],
            )
        except Exception as e:
            return ReportAdvertisementResponse(success=False, message=f"Error creating flag: {str(e)}")
            
        print("ad.is_reported:", ad.is_reported)
        if ad.is_reported:
            ad.admin_actions = "flagged"
            ad.save(update_fields=["admin_actions"])

        # ── Notifications: Ad flagged by admin ────────────────────────────
        from api.notification_service import create_notification
        # Notify the business owner
        create_notification(
            recipient=ad.user,
            notification_type="ad_flagged_by_admin",
            title="Your advertisement has been flagged",
            message=f'Your ad "{ad.advertisment_name}" has been flagged for policy review.',
            advertisement_id=ad.id,
        )
        # Notify admins
        for admin in User.objects.filter(role="admin", is_active=True).exclude(id=user.id):
            create_notification(
                recipient=admin,
                notification_type="ad_flagged",
                title="Advertisement flagged",
                message=f'Ad "{ad.advertisment_name}" (ID: {ad.id}) has been flagged by {user.email}.',
                advertisement_id=ad.id,
                recipient_role="admin",
            )

        return ReportAdvertisementResponse(success=True, message="Advertisement has been reported successfully.")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def report_offer(self, info, input: ReportInput) -> ReportAdvertisementResponse:
        request = info.context.request
        user = request.user

        try:
            ad = Advertisement.objects.get(pk=input.entityId)
        except Advertisement.DoesNotExist:
            return ReportAdvertisementResponse(success=False, message="Advertisement not found.")

        # Prevent same user from reporting again
        if AdvertisementReports.objects.filter(advertisement=ad, user=user).exists():
            return ReportAdvertisementResponse(success=False, message="You have already reported this advertisement.")
        report_id = generate_report_id()

    # Make sure it's unique
        while AdvertisementReports.objects.filter(report_id=report_id).exists():
            report_id = generate_report_id()
        # Create the new report
        AdvertisementReports.objects.create(
            advertisement=ad,
            user=user,
            status="pending",  # Initial status
            reason=input.reason,
            report_id=report_id
        )
        forty_eight_hours_ago = timezone.now() - timedelta(hours=48)

        recent_reports_count = AdvertisementReports.objects.filter(
            advertisement=ad,
            created_at__gte=forty_eight_hours_ago  # reports from last 48h
        ).count()

        # Set escalation flag
        escalation_needed = recent_reports_count >= 5
        print("escalation_needed",escalation_needed)
        # If this is the *first* report ever for this ad, set first_report_date
        if not ad.first_report_date:
            ad.first_report_date = timezone.now()

        ad.admin_actions = "action not taken"
        ad.is_ever_reported = True
        ad.is_reported = True
        ad.escalation = escalation_needed
        ad.save(update_fields=["admin_actions",
                "is_reported", "first_report_date", "is_ever_reported","escalation"])

        # ── Notifications: Ad reported by user ────────────────────────────
        from api.notification_service import create_notification
        # Notify admins
        for admin in User.objects.filter(role="admin", is_active=True):
            create_notification(
                recipient=admin,
                notification_type="ad_reported",
                title="Advertisement reported by user",
                message=f'Ad "{ad.advertisment_name}" (ID: {ad.id}) was reported. Reason: {input.reason}',
                advertisement_id=ad.id,
                recipient_role="admin",
            )
        # Notify the business owner
        create_notification(
            recipient=ad.user,
            notification_type="ad_reported",
            title="Your advertisement has been reported",
            message=f'Your ad "{ad.advertisment_name}" has been reported by a user.',
            advertisement_id=ad.id,
        )

        return ReportAdvertisementResponse(success=True, message="Advertisement has been reported successfully.")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def ignore_report(self, info: Info, id: int, type: str) -> ReportAdvertisementResponse:
        user = info.context.request.user
        if user.role != "admin":
            raise GraphQLError("Only admins can perform this action.")

        try:
            if type == "Advertisement":
                item = Advertisement.objects.get(id=id)
                AdvertisementReports.objects.filter(advertisement=item).update(status="ignored")

                if not AdvertisementReports.objects.filter(advertisement=item, status="pending").exists():
                    item.is_reported = False
                    item.save(update_fields=["is_reported"])

            elif type == "Listing":
                item = Listings.objects.get(id=id)
                ListingReports.objects.filter(listing=item).update(status="ignored")

                if not ListingReports.objects.filter(listing=item, status="pending").exists():
                    item.is_reported = False
                    item.save(update_fields=["is_reported"])
            else:
                raise GraphQLError("Invalid type.")

            return ReportAdvertisementResponse(success=True, message=f"{type} report ignored successfully.")
        except Exception as e:
            raise GraphQLError(f"Error ignoring report: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def resolve_report(self, info: Info, id: int, type: str) -> ReportAdvertisementResponse:
        user = info.context.request.user
        if user.role != "admin":
            raise GraphQLError("Only admins can perform this action.")

        try:
            if type == "Advertisement":
                item = Advertisement.objects.get(id=id)
                item.admin_actions = "resolved"
                item.save(update_fields=["admin_actions"])

                AdvertisementReports.objects.filter(advertisement=item).update(status="resolved")

                if not AdvertisementReports.objects.filter(advertisement=item, status="pending").exists():
                    item.is_reported = False
                    item.save(update_fields=["is_reported"])

            elif type == "Listing":
                item = Listings.objects.get(id=id)
                item.admin_actions = "resolved"
                item.save(update_fields=["admin_actions"])

                ListingReports.objects.filter(listing=item).update(status="resolved")

                if not ListingReports.objects.filter(listing=item, status="pending").exists():
                    item.is_reported = False
                    item.save(update_fields=["is_reported"])
            else:
                raise GraphQLError("Invalid type.")

            return ReportAdvertisementResponse(success=True, message=f"{type} report resolved successfully.")
        except Exception as e:
            raise GraphQLError(f"Error resolving report: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def ignore_all_reports_by_user(self, info: Info, user_id: int) -> ReportAdvertisementResponse:
        user = info.context.request.user
        if user.role != "admin":
            raise GraphQLError("Only admins can perform this action.")

        try:
            target_user = User.objects.get(id=user_id)

            ad_reports = AdvertisementReports.objects.filter(user=target_user, status="pending").select_related("advertisement")
            listing_reports = ListingReports.objects.filter(user=target_user, status="pending").select_related("listing")

            affected_ads = list(set(r.advertisement for r in ad_reports))
            affected_listings = list(set(r.listing for r in listing_reports))

            ad_reports.update(status="ignored")
            listing_reports.update(status="ignored")

            for ad in affected_ads:
                if not AdvertisementReports.objects.filter(advertisement=ad, status="pending").exists():
                    ad.is_reported = False
                    ad.save(update_fields=["is_reported"])

            for listing in affected_listings:
                if not ListingReports.objects.filter(listing=listing, status="pending").exists():
                    listing.is_reported = False
                    listing.save(update_fields=["is_reported"])

            return ReportAdvertisementResponse(success=True, message=f"All reports by {target_user.email} ignored successfully.")
        except Exception as e:
            raise GraphQLError(f"Error ignoring reports: {str(e)}")

    # report listings not flag
    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def report_user_listing(self, info, input: ReportInput) -> ReportListingResponse:
        request = info.context.request
        user = request.user

        try:
            listing = Listings.objects.get(pk=input.entityId)
        except Listings.DoesNotExist:
            return ReportListingResponse(success=False, message="Listing not found.")

        # Prevent same user from reporting again
        if ListingReports.objects.filter(listing=listing, user=user).exists():
            return ReportListingResponse(success=False, message="You have already reported this listing.")
        report_id = generate_report_id()

        # Make sure it's unique
        while ListingReports.objects.filter(report_id=report_id).exists():
            report_id = generate_report_id()
        # Create the new report
        ListingReports.objects.create(
            listing=listing,
            user=user,
            status="pending",  # Initial status
            reason=input.reason, report_id=report_id
        )

        forty_eight_hours_ago = timezone.now() - timedelta(hours=48)

        recent_reports_count = ListingReports.objects.filter(
            listing=listing,
            created_at__gte=forty_eight_hours_ago  # reports from last 48h
        ).count()

        # Set escalation flag
        escalation_needed = recent_reports_count >= 5

        # ✅ Update advertisement fields
        # If this is the *first* report ever for this ad, set first_report_date
        if not listing.first_report_date:
            listing.first_report_date = timezone.now()

        listing.admin_actions = "action not taken"
        listing.is_ever_reported = True
        listing.is_reported = True
        listing.escalation = escalation_needed
        listing.save(update_fields=["admin_actions",
                                    "is_reported", "first_report_date", "is_ever_reported","escalation"])

        # ── Notifications: Listing reported by user ───────────────────────
        from api.notification_service import create_notification
        # Notify admins
        for admin in User.objects.filter(role="admin", is_active=True):
            create_notification(
                recipient=admin,
                notification_type="listing_reported",
                title="Listing reported by user",
                message=f'Listing "{listing.listing_name}" (ID: {listing.id}) was reported.',
                recipient_role="admin",
            )
        # Notify the business owner
        create_notification(
            recipient=listing.user,
            notification_type="listing_reported",
            title="Your listing has been reported",
            message=f'Your listing "{listing.listing_name}" has been reported by a user.',
        )

        return ReportAdvertisementResponse(success=True, message="Advertisement has been reported successfully.")
    # this api is for flag listing

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def report_listing(self, info, input: ReportListingInput) -> ReportListingResponse:
        request = info.context.request
        user = request.user

        try:
            listing = Listings.objects.get(pk=input.listing_id)
        except Listings.DoesNotExist:
            return ReportListingResponse(success=False, message="Listing not found.")

        if hasattr(listing, "flag"):
            return ReportListingResponse(success=False, message="This listing has already been reported.")

        # Mark as flagged
        
        listing.save()

        # Remove existing flag entry if it exists
        ListingFlag.objects.filter(listing=listing).delete()

        # Create new flag
        ListingFlag.objects.create(
            listing=listing,
            flagged_by=user,
            title=input.title or "",
            description=input.description or "",
            images=input.images or [],
        )

        # ── Notifications: Listing flagged by admin ───────────────────────
        from api.notification_service import create_notification
        # Notify the business owner
        create_notification(
            recipient=listing.user,
            notification_type="listing_flagged",
            title="Your listing has been flagged",
            message=f'Your listing "{listing.listing_name}" has been flagged for policy review.',
        )
        # Notify admins
        for admin in User.objects.filter(role="admin", is_active=True).exclude(id=user.id):
            create_notification(
                recipient=admin,
                notification_type="listing_flagged",
                title="Listing flagged",
                message=f'Listing "{listing.listing_name}" (ID: {listing.id}) has been flagged by {user.email}.',
                recipient_role="admin",
            )
        if listing.is_reported:
            listing.admin_actions = "flagged"
            listing.save(update_fields=["admin_actions"])

        return ReportListingResponse(success=True, message="Listing has been reported successfully.")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def resolve_advertisement_flag(self, info, input: ResolveAdvertisementInput) -> ReportAdvertisementResponse:
        request = info.context.request
        user = request.user

        try:
            ad = Advertisement.objects.get(pk=input.advertisement_id)
        except Advertisement.DoesNotExist:
            return ReportAdvertisementResponse(success=False, message="Advertisement not found.")

        flag = ad.active_flag
        if not flag:
            return ReportAdvertisementResponse(success=False, message="This advertisement is not flagged.")

        if flag.resolved:
            return ReportAdvertisementResponse(success=False, message="This advertisement is already resolved.")

        flag.resolved = True
        flag.resolved_at = timezone.now()
        flag.save()

        return ReportAdvertisementResponse(success=True, message="Flag has been resolved successfully.")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def resolve_listing_flag(self, info, input: ResolveListingInput) -> ResolveListingResponse:
        request = info.context.request
        user = request.user

        try:
            listing = Listings.objects.get(pk=input.listing_id)
        except Listings.DoesNotExist:
            return ResolveListingResponse(success=False, message="Listing not found.")

        try:
            flag = listing.flag  # Assuming OneToOneField from Listing to ListingFlag
        except ListingFlag.DoesNotExist:
            return ResolveListingResponse(success=False, message="This listing is not flagged.")

        if flag.resolved:
            return ResolveListingResponse(success=False, message="This listing is already resolved.")

        flag.resolved = True
        flag.save()

        listing.is_ever_flagged = False
        listing.save(update_fields=["is_ever_flagged"])
        return ResolveListingResponse(success=True, message="Flag has been resolved successfully.")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    # write a code to save location in client location model
    def save_location(self, info: Info, location_type: str, latitude: float, longitude: float, address: str, city: str, country: str, postal_code: Optional[str] = None) -> LocationSaveRespone:
        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated first")
            if not hasattr(user, 'client_profile'):
                raise GraphQLError(
                    "User does not have a client profile to save location.")

            client_profile = user.client_profile

            # Check if location_type already exists for this client
            if ClientLocation.objects.filter(client=client_profile, location_type=location_type).exists():
                raise GraphQLError(
                    f"Location with type '{location_type}' is already saved.")

            # Save the new location
            ClientLocation.objects.create(
                client=client_profile,
                latitude=latitude,
                longitude=longitude,
                address=address,
                city=city,
                country=country,
                postal_code=postal_code,
                location_type=location_type
            )

            return LocationSaveRespone(success=True, message="Location saved successfully.")

        except Exception as e:
            raise GraphQLError(f"Error saving location: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def update_location(
        self,
        info: Info,
        location_id: int,
        location_type: Optional[str] = None,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        address: Optional[str] = None,
        city: Optional[str] = None,
        country: Optional[str] = None,
        postal_code: Optional[str] = None,
    ) -> LocationSaveRespone:
        from api.models import ClientLocation

        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, 'client_profile'):
                raise GraphQLError("User does not have a client profile.")

            client_profile = user.client_profile

            location = ClientLocation.objects.filter(
                id=location_id, client=client_profile).first()
            if not location:
                raise GraphQLError(
                    "Location not found or does not belong to the client.")

            # Use a loop to update fields dynamically
            update_fields = {
                "location_type": location_type,
                "latitude": latitude,
                "longitude": longitude,
                "address": address,
                "city": city,
                "country": country,
                "postal_code": postal_code,
            }

            for field, value in update_fields.items():
                if value is not None:
                    setattr(location, field, value)

            location.save()

            return LocationSaveRespone(success=True, message="Location updated successfully.")

        except Exception as e:
            raise GraphQLError(f"Error updating location: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def update_client_profile(
        self,
        info,
        input: UpdateClientProfileInput
    ) -> ClientProfileType:
        from api.models import AdvertisementChannel, ClientChannelSelection

        try:
            user = info.context.request.user

            if not hasattr(user, 'client_profile'):
                raise GraphQLError(
                    "User does not have a client profile to update.")

            client_profile = user.client_profile

            for field in vars(input):
                value = getattr(input, field, None)

                if value is not None:
                    if field == "selected_channels":
                        ClientChannelSelection.objects.filter(
                            client=client_profile).delete()
                        for channel_id in value:
                            channel = AdvertisementChannel.objects.filter(
                                id=channel_id).first()
                            if not channel:
                                continue  # Or raise an error if strict validation is needed

                            exists = ClientChannelSelection.objects.filter(
                                client=client_profile,
                                channel=channel
                            ).exists()

                            if not exists:
                                ClientChannelSelection.objects.create(
                                    client=client_profile,
                                    channel=channel
                                )
                    else:
                        setattr(client_profile, field, value)

            client_profile.save()

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
            raise GraphQLError(f"Error updating client profile: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def delete_location(self, info: Info, location_id: int) -> LocationSaveRespone:

        try:
            user = info.context.request.user

            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, 'client_profile'):
                raise GraphQLError("User does not have a client profile.")

            client_profile = user.client_profile

            location = ClientLocation.objects.filter(
                id=location_id, client=client_profile).first()
            if not location:
                raise GraphQLError(
                    "Location not found or does not belong to the client.")

            location.delete()

            return LocationSaveRespone(success=True, message="Location deleted successfully.")

        except Exception as e:
            raise GraphQLError(f"Error deleting location: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def liked_business_Profile(self, info: Info, business_Profile_id: int) -> LikedRespone:
        try:
            user = info.context.request.user

            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated first")

            if not hasattr(user, "client_profile"):
                raise GraphQLError("User does not have a profile.")
            profile = user.client_profile
            business_Profile = BusinessProfile.objects.get(
                id=business_Profile_id)
            if not business_Profile:
                raise GraphQLError("Business Profile not found.")
            if LikedBusinessProfile.objects.filter(profile=profile, business_profile=business_Profile).exists():
                raise GraphQLError(
                    "You have already liked this business profile.")
            LikedBusinessProfile.objects.create(
                profile=profile, business_profile=business_Profile)
            return LikedRespone(success=True, message="Business profile liked successfully.")
        except Exception as e:
            raise GraphQLError(f"Error liking business profile: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def unsave_business_profile(self, info: Info, business_profile_id: int) -> LikedRespone:
        try:
            user = info.context.request.user

            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, 'client_profile'):
                raise GraphQLError("User does not have a profile.")

            profile = user.client_profile

            liked_item = LikedBusinessProfile.objects.filter(
                profile=profile,
                business_profile_id=business_profile_id
            ).first()

            if not liked_item:
                raise GraphQLError(
                    "This business profile is not saved by the user.")

            liked_item.delete()

            return LikedRespone(
                success=True,
                message="Business profile unsaved successfully."
            )

        except Exception as e:
            raise GraphQLError(f"Error unsaving business profile: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def unsave_all_business_profiles(self, info: Info) -> UnsaveAllBusinessProfilesResponse:
        try:
            user = info.context.request.user

            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, 'client_profile'):
                raise GraphQLError("User does not have a profile.")

            profile = user.client_profile

            deleted_count, _ = LikedBusinessProfile.objects.filter(
                profile=profile).delete()

            return UnsaveAllBusinessProfilesResponse(
                success=True,
                message="All business profiles unsaved successfully.",
                count=deleted_count
            )

        except Exception as e:
            raise GraphQLError(
                f"Error unsaving all business profiles: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def like_advertisement(self, info: Info, advertisement_id: int) -> LikedRespone:
        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, "client_profile"):
                raise GraphQLError("User does not have a client profile.")

            client_profile = user.client_profile
            ad = Advertisement.objects.filter(id=advertisement_id).first()
            if not ad:
                raise GraphQLError("Advertisement not found.")

            if LikedAdvertisement.objects.filter(client_profile=client_profile, advertisement=ad).exists():
                raise GraphQLError(
                    "You have already liked this advertisement.")

            LikedAdvertisement.objects.create(
                client_profile=client_profile, advertisement=ad)

            return LikedRespone(success=True, message="Advertisement liked successfully.")

        except Exception as e:
            raise GraphQLError(f"Error liking advertisement: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def unlike_advertisement(self, info: Info, advertisement_id: int) -> LikedRespone:
        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, "client_profile"):
                raise GraphQLError("User does not have a client profile.")

            client_profile = user.client_profile
            like_entry = LikedAdvertisement.objects.filter(
                client_profile=client_profile, advertisement_id=advertisement_id).first()
            if not like_entry:
                raise GraphQLError("You have not liked this advertisement.")

            like_entry.delete()
            return LikedRespone(success=True, message="Advertisement unliked successfully.")

        except Exception as e:
            raise GraphQLError(f"Error unliking advertisement: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def unlike_all_advertisements(self, info: Info) -> LikedRespone:
        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, "client_profile"):
                raise GraphQLError("User does not have a client profile.")

            client_profile = user.client_profile
            deleted_count, _ = LikedAdvertisement.objects.filter(
                client_profile=client_profile).delete()

            return LikedRespone(success=True, message=f"All liked advertisements removed. Total: {deleted_count}")

        except Exception as e:
            raise GraphQLError(f"Error unliking all advertisements: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def like_advertisement_channel(self, info: Info, channel_id: int) -> LikedRespone:
        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, "client_profile"):
                raise GraphQLError("User does not have a client profile.")

            client_profile = user.client_profile
            channel = AdvertisementChannel.objects.filter(
                id=channel_id).first()
            if not channel:
                raise GraphQLError("Advertisement Channel not found.")

            if LikedAdvertisementChannel.objects.filter(client_profile=client_profile, advertisement_channel=channel).exists():
                raise GraphQLError(
                    "You have already liked this advertisement channel.")

            LikedAdvertisementChannel.objects.create(
                client_profile=client_profile, advertisement_channel=channel)

            return LikedRespone(success=True, message="Advertisement channel liked successfully.")

        except Exception as e:
            raise GraphQLError(f"Error liking advertisement channel: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def unlike_advertisement_channel(self, info: Info, channel_id: int) -> LikedRespone:
        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, "client_profile"):
                raise GraphQLError("User does not have a client profile.")

            client_profile = user.client_profile
            like_entry = LikedAdvertisementChannel.objects.filter(
                client_profile=client_profile,
                advertisement_channel_id=channel_id
            ).first()
            if not like_entry:
                raise GraphQLError(
                    "You have not liked this advertisement channel.")

            like_entry.delete()
            return LikedRespone(success=True, message="Advertisement channel unliked successfully.")

        except Exception as e:
            raise GraphQLError(
                f"Error unliking advertisement channel: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def unlike_all_advertisement_channels(self, info: Info) -> LikedRespone:
        try:
            user = info.context.request.user
            if not user.is_authenticated:
                raise GraphQLError("User must be authenticated.")

            if not hasattr(user, "client_profile"):
                raise GraphQLError("User does not have a client profile.")

            client_profile = user.client_profile
            deleted_count, _ = LikedAdvertisementChannel.objects.filter(
                client_profile=client_profile).delete()

            return LikedRespone(success=True, message=f"All liked advertisement channels removed. Total: {deleted_count}")

        except Exception as e:
            raise GraphQLError(
                f"Error unliking all advertisement channels: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def send_support_email_to_admin_and_business(
        self, info: Info,
        input: SupportEmailInput
    ) -> SupportEmailResponse:

        try:
            user = info.context.request.user
            # Extract business details from input OR fallback to user profile
            business_name = input.business_name or "Not Available"
            business_email = input.business_email or getattr(user, "email", "Not Available")
            business_uuid = input.business_uuid or "Not Available"

            if business_name == "Not Available" or business_uuid == "Not Available":
                b_profile = getattr(user, "business_profile", None)
                if b_profile:
                    if business_name == "Not Available":
                        business_name = getattr(b_profile, "business_name", "Not Available")
                    if business_uuid == "Not Available":
                        business_uuid = str(getattr(b_profile, "business_uuid", "Not Available"))

            if business_email == "Not Available":
                raise ValueError(
                    "Business email not found.")
            images_html = ""
            images_text = ""
            # Admin email (hardcoded or from settings)
            admin_email = "services@twam.com"

            # Extract fields from input
            subject = input.subject
            full_name = input.full_name
            designation = input.designation
            message = input.message
            images = input.images or []

            if images:
                images_text = "\n\nAttached Images:\n" + "\n".join(images)
                images_html = "<br><p><b>Attached Images:</b></p>" + "".join(
                    [f'<p><a href="{url}">{url}</a></p>' for url in images]
                )
            # Generate ticket number
            ticket_number = f"TCKT-{uuid.uuid4().hex[:8].upper()}"

            email_sender = SESEmailSender()

            # ---- Email for Admin ----
            admin_subject = f"[Support Ticket {ticket_number}] {subject}"


            admin_body_text = (
                f"A new support request has been submitted.\n\n"
                f"Ticket No: {ticket_number}\n\n"
                f"Subject: {subject}\n"
                f"Name: {full_name}\n"
                f"Designation: {designation}\n\n"
                f"Business Info:\n"
                f"Name: {business_name}\n"
                f"Email: {business_email}\n"
                f"UUID: {business_uuid}\n\n"
                f"Message:\n{message}\n\n"
                f"Images:\n" + ("\n".join(images)
                                if images else "No images attached")
            )

            admin_body_html = f"""
            <html><body>
                <p>A new support request has been submitted.</p>
                <p><b>Ticket No:</b> {ticket_number}</p>
                <p><b>Subject:</b> {subject}</p>
                <p><b>Name:</b> {full_name}</p>
                <p><b>Designation:</b> {designation}</p>
                <hr>
                <p><b>Business Info:</b></p>
                <p><b>Name:</b> {business_name}</p>
                <p><b>Email:</b> {business_email}</p>
                <p><b>UUID:</b> {business_uuid}</p>
                <hr>
                <p><b>Message:</b></p>
                <p>{message}</p>
                <p><b>Images:</b></p>
                {"".join([f'<p><a href="{url}">{url}</a></p>' for url in images])
                 if images else "<p>No images attached</p>"}
            </body></html>
            """

            email_sender.send_email(
                recipient=admin_email,
                subject=admin_subject,
                body_text=admin_body_text,
                body_html=admin_body_html,
            )

            # ---- Copy for Business Owner ----
            business_subject = f"Copy of Your Support Ticket {ticket_number}"
            business_body_text = (
                f"Hello {full_name},\n\n"
                f"We have received your support request.\n\n"
                f"Ticket Number: {ticket_number}\n"
                f"Subject: {subject}\n\n"
                f"Message:\n{message}\n"
                f"{images_text}\n\n"
                f"We will respond shortly.\n"
            )
            business_body_html = f"""
                <html><body>
                    <p>Hello {full_name},</p>
                    <p>We have received your support request.</p>
                    <p><b>Ticket Number:</b> {ticket_number}</p>
                    <p><b>Subject:</b> {subject}</p>
                    <p><b>Message:</b></p>
                    <p>{message}</p>
                    {images_html}
                    <br>
                    <p><i>We will respond shortly.</i></p>
                </body></html>
                """

            email_sender.send_email(
                recipient=business_email,
                subject=business_subject,
                body_text=business_body_text,
                body_html=business_body_html,
            )

            if input.subject in ["Account Suspension Inquiry", "Account Reactivation Request"]:
                business_profile = getattr(user, "business_profile", None)

                if not business_profile:
                    raise Exception("Business profile not found for this user")
                suspensions = business_profile.suspension.all()
                latest_suspension = suspensions.order_by("-start_date").first()
                if latest_suspension and business_profile.is_suspended:
                    latest_suspension.business_response = input.subject
                    latest_suspension.save()
            ticket = SupportTicket.objects.create(
                ticket_number=ticket_number,
                subject=subject,
                full_name=full_name,
                designation=designation,
                message=message,
                images=images,
                business=getattr(user, "business_profile", None),
                business_name=business_name,
                business_email=business_email,
                business_uuid=business_uuid
            )

            return SupportEmailResponse(
                success=True,
                message="Support email sent successfully.",
                ticket_number=ticket_number
            )

        except Exception as e:
            raise GraphQLError(
                f"Error unliking all advertisement channels: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def end_listing(self, info: Info, listing_id: str) -> SuccessEndListingType:
        try:
            user = info.context.request.user

            # Find the listing
            listing = Listings.objects.get(id=listing_id)

            # Update status to phaseout
            listing.listing_status = "phaseout"
            listing.save()

            return SuccessEndListingType(
                id=listing.id,
                message="Listing status changed to phaseout successfully."
            )

        except Listings.DoesNotExist:
            raise GraphQLError("Listing not found.")
        except Exception as e:
            raise GraphQLError(f"Error updating listing status: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def suspend_business(self, info, input: CreateSuspensionInput) -> SuspensionType:
        try:
            user = info.context.request.user
            business = (
                BusinessProfile.objects
                .select_related("user", "category")
                .prefetch_related(
                    "business_hours",
                    "user__advertisements",
                    "user__listings"   # 👈 prefetch listings for this business
                )
                .get(user__id=input.business_id)
            )

            business.is_suspended = True

            business.user.advertisements.filter(
                Q(advertisement_status="active") | Q(is_ever_flagged=True)
            ).update(advertisement_status="phaseout", is_ever_flagged=False)

            # For listings
            business.user.listings.filter(
                Q(listing_status="active") | Q(is_ever_flagged=True)
            ).update(listing_status="phaseout", is_ever_flagged=False)

            business.save(update_fields=["is_suspended"])

            suspension = Suspension.objects.create(
                business=business, suspension_reason=input.reason, note=input.note, suspended_by=user, action="suspended", is_active=True)

            # Notify: account suspended
            from api.notification_service import create_notification
            create_notification(
                recipient=business.user,
                notification_type="account_suspended",
                title="Your business account has been suspended",
                message=f"Your business account has been suspended by an administrator. Reason: {input.reason or 'Not specified'}. All your active ads and listings have been phased out.",
            )

            return SuspensionType(business=business, suspension_reason=suspension.suspension_reason, note=suspension.note, suspended_by=user, start_date=suspension.start_date, end_date=suspension.end_date, id=suspension.id, is_active=True, action=suspension.action)
        except BusinessProfile.DoesNotExist:
            raise GraphQLError("Business not found.")

        except Exception as e:
            raise GraphQLError(f"Error suspending business: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def revert_suspended_business(self, info, userId: str) -> RevertSuspendedBusinessReturnType:
        try:
            user = info.context.request.user

            business = (
                BusinessProfile.objects
                .get(user__id=userId)
            )

            business.is_suspended = False

            business.save(update_fields=["is_suspended"])

            suspension = Suspension.objects.create(
                business=business, suspension_reason=None, note=None, suspended_by=None, action="reactivated", reactivated_by=user, is_active=False)

            # Notify: account reactivated
            from api.notification_service import create_notification
            create_notification(
                recipient=business.user,
                notification_type="business_approved",
                title="Your business account has been reactivated",
                message="Your business account has been reactivated by an administrator. You can now create ads and listings again.",
            )

            return RevertSuspendedBusinessReturnType(message="Suspension removed successfully", success=True)
        except BusinessProfile.DoesNotExist:
            raise GraphQLError("Business not found.")

        except Exception as e:
            raise GraphQLError(f"Error suspending business: {str(e)}")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_role(["business"])
    @require_settings_verified
    def set_default_payment_method(self, info: Info, payment_method_id: str) -> DefaultPaymentMethodResponse:
        try:
            user = info.context.request.user
            success = set_customer_default_payment_method(user, payment_method_id)
            if success:
                return DefaultPaymentMethodResponse(success=True, message="Default payment method updated successfully.")
            else:
                return DefaultPaymentMethodResponse(success=False, message="Failed to update default payment method.")
        except Exception as e:
             logger.error(f"Error setting default payment method: {str(e)}")
             return DefaultPaymentMethodResponse(success=False, message=str(e))

    # ─── Payment OTP ────────────────────────────────────────────────────────────

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def request_payment_otp(self, info: Info) -> RequestPaymentOtpResponse:
        """
        Initiates the OTP flow for accessing Payment Methods settings.
        Sends a fresh OTP to the user's registered email.
        """
        try:
            user = info.context.request.user
            generate_and_send_otp(user, purpose="Payment Method Update")
            return RequestPaymentOtpResponse(success=True, message="OTP sent to your registered email.")
        except Exception as e:
            raise GraphQLError(str(e))

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def resend_payment_otp(self, info: Info) -> RequestPaymentOtpResponse:
        """
        Resends the payment OTP. Subject to the existing resend interval / cooldown
        but does NOT reset the failed-attempt counter.
        """
        try:
            user = info.context.request.user
            generate_and_send_otp(user, purpose="Payment Method Update")
            return RequestPaymentOtpResponse(success=True, message="A new OTP has been sent to your email.")
        except Exception as e:
            raise GraphQLError(str(e))

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def verify_payment_otp(self, info: Info, otp_code: str) -> VerifyPaymentOtpResponse:
        """
        Verifies the OTP entered by the user for the Payment Methods screen.

        - Correct OTP: resets attempt counter, returns a one-time payment_session_token.
        - Wrong OTP: increments attempt counter.
        - 3rd wrong attempt: sends security email + blacklists all refresh tokens (logout).
        """
        import uuid as _uuid
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken

        MAX_PAYMENT_OTP_ATTEMPTS = 3

        user = info.context.request.user

        # ── 1. Retrieve attempt record ───────────────────────────────────────────
        attempt_record, _ = PaymentOtpAttempt.objects.get_or_create(user=user)

        # ── 2. Guard: already locked out ────────────────────────────────────────
        if attempt_record.attempt_count >= MAX_PAYMENT_OTP_ATTEMPTS:
            return VerifyPaymentOtpResponse(
                success=False,
                message="LOCKED_OUT"
            )

        # ── 3. Look up the OTP record ────────────────────────────────────────────
        otp_record = OTP.objects.filter(user=user, otp_code=otp_code).first()

        if not otp_record or otp_record.is_expired():
            # ── Wrong / expired OTP ──────────────────────────────────────────────
            attempt_record.attempt_count += 1
            attempt_record.save()

            remaining = MAX_PAYMENT_OTP_ATTEMPTS - attempt_record.attempt_count

            if remaining > 0:
                return VerifyPaymentOtpResponse(
                    success=False,
                    message=f"Invalid OTP. {remaining} attempt(s) remaining."
                )

            # ── 3rd failure: security email + full session termination ────────────
            attempt_record.locked_at = timezone.now()
            attempt_record.save()

            # Send security warning email
            full_name = f"{user.first_name} {user.last_name}"
            email_sender = SESEmailSender()
            security_subject = "Security Alert: Suspicious Activity on Your Account"
            security_body_text = (
                f"Hello {full_name},\n\n"
                f"We detected 3 failed attempts to access your payment methods.\n"
                f"If this was not you, your account may be compromised.\n\n"
                f"Please reset your password immediately via account recovery:\n"
                f"Go to the login page and click 'Forgot Password'.\n\n"
                f"If this was you, you can ignore this email.\n\n"
                f"The TWAM Team"
            )
            security_body_html = f"""
            <html><body>
                <p>Hello {full_name},</p>
                <p>We detected <strong>3 failed attempts</strong> to access your payment methods.</p>
                <p>If this was <strong>not you</strong>, your account may be compromised.</p>
                <p><strong>Please reset your password immediately via account recovery.</strong><br>
                Go to the login page and click <em>Forgot Password</em>.</p>
                <p>If this was you, you can ignore this email.</p>
                <br>
                <p>The TWAM Team</p>
            </body></html>
            """
            try:
                email_sender.send_email(
                    recipient=user.email,
                    subject=security_subject,
                    body_text=security_body_text,
                    body_html=security_body_html,
                )
            except Exception as mail_err:
                logger.error(f"Failed to send security email for user {user.email}: {mail_err}")

            # Blacklist all outstanding refresh tokens for this user
            try:
                outstanding_tokens = OutstandingToken.objects.filter(user=user)
                for token in outstanding_tokens:
                    BlacklistedToken.objects.get_or_create(token=token)
            except Exception as blacklist_err:
                logger.error(f"Failed to blacklist tokens for user {user.email}: {blacklist_err}")

            return VerifyPaymentOtpResponse(
                success=False,
                message="LOCKED_OUT"
            )

        # ── 4. Correct OTP ───────────────────────────────────────────────────────
        otp_record.delete()
        attempt_record.attempt_count = 0
        attempt_record.locked_at = None
        attempt_record.save()

        payment_session_token = str(_uuid.uuid4())
        return VerifyPaymentOtpResponse(
            success=True,
            message="OTP verified successfully.",
            payment_session_token=payment_session_token
        )


    # ─── Password OTP ────────────────────────────────────────────────────────────

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def request_password_otp(self, info: Info) -> RequestPasswordOtpResponse:
        """
        Initiates the OTP flow for accessing Password Update.
        Sends a fresh OTP to the user's registered email.
        """
        try:
            user = info.context.request.user
            generate_and_send_otp(user, purpose="Password Update")
            return RequestPasswordOtpResponse(success=True, message="OTP sent to your registered email.")
        except Exception as e:
            raise GraphQLError(str(e))

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def resend_password_otp(self, info: Info) -> RequestPasswordOtpResponse:
        """
        Resends the password OTP. Subject to the existing resend interval / cooldown
        but does NOT reset the failed-attempt counter.
        """
        try:
            user = info.context.request.user
            generate_and_send_otp(user, purpose="Password Update")
            return RequestPasswordOtpResponse(success=True, message="A new OTP has been sent to your email.")
        except Exception as e:
            raise GraphQLError(str(e))

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def verify_password_otp(self, info: Info, otp_code: str) -> VerifyPasswordOtpResponse:
        """
        Verifies the OTP entered by the user for the Password Update screen.

        - Correct OTP: resets attempt counter, returns a one-time password_session_token.
        - Wrong OTP: increments attempt counter.
        - 3rd wrong attempt: sends security email + blacklists all refresh tokens (logout).
        """
        import uuid as _uuid
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken

        MAX_PASSWORD_OTP_ATTEMPTS = 3

        user = info.context.request.user

        # ── 1. Retrieve attempt record ───────────────────────────────────────────
        attempt_record, _ = PasswordOtpAttempt.objects.get_or_create(user=user)

        # ── 2. Guard: already locked out ────────────────────────────────────────
        if attempt_record.attempt_count >= MAX_PASSWORD_OTP_ATTEMPTS:
            return VerifyPasswordOtpResponse(
                success=False,
                message="LOCKED_OUT"
            )

        # ── 3. Look up the OTP record ────────────────────────────────────────────
        otp_record = OTP.objects.filter(user=user, otp_code=otp_code).first()

        if not otp_record or otp_record.is_expired():
            # ── Wrong / expired OTP ──────────────────────────────────────────────
            attempt_record.attempt_count += 1
            attempt_record.save()

            remaining = MAX_PASSWORD_OTP_ATTEMPTS - attempt_record.attempt_count

            if remaining > 0:
                return VerifyPasswordOtpResponse(
                    success=False,
                    message=f"Invalid OTP. {remaining} attempt(s) remaining."
                )

            # ── 3rd failure: security email + full session termination ────────────
            attempt_record.locked_at = timezone.now()
            attempt_record.save()

            # Send security warning email
            full_name = f"{user.first_name} {user.last_name}"
            email_sender = SESEmailSender()
            security_subject = "Security Alert: Suspicious Activity on Your Account"
            security_body_text = (
                f"Hello {full_name},\n\n"
                f"We detected 3 failed attempts to access your password update page.\n"
                f"If this was not you, your account may be compromised.\n\n"
                f"Please reset your password immediately via account recovery:\n"
                f"Go to the login page and click 'Forgot Password'.\n\n"
                f"If this was you, you can ignore this email.\n\n"
                f"The TWAM Team"
            )
            security_body_html = f"""
            <html><body>
                <p>Hello {full_name},</p>
                <p>We detected <strong>3 failed attempts</strong> to access your password update page.</p>
                <p>If this was <strong>not you</strong>, your account may be compromised.</p>
                <p><strong>Please reset your password immediately via account recovery.</strong><br>
                Go to the login page and click <em>Forgot Password</em>.</p>
                <p>If this was you, you can ignore this email.</p>
                <br>
                <p>The TWAM Team</p>
            </body></html>
            """
            try:
                email_sender.send_email(
                    recipient=user.email,
                    subject=security_subject,
                    body_text=security_body_text,
                    body_html=security_body_html,
                )
            except Exception as mail_err:
                logger.error(f"Failed to send security email for user {user.email}: {mail_err}")

            # Blacklist all outstanding refresh tokens for this user
            try:
                outstanding_tokens = OutstandingToken.objects.filter(user=user)
                for token in outstanding_tokens:
                    BlacklistedToken.objects.get_or_create(token=token)
            except Exception as blacklist_err:
                logger.error(f"Failed to blacklist tokens for user {user.email}: {blacklist_err}")

            return VerifyPasswordOtpResponse(
                success=False,
                message="LOCKED_OUT"
            )

        # ── 4. Correct OTP ───────────────────────────────────────────────────────
        otp_record.delete()
        attempt_record.attempt_count = 0
        attempt_record.locked_at = None
        attempt_record.save()

        password_session_token = str(_uuid.uuid4())
        return VerifyPasswordOtpResponse(
            success=True,
            message="OTP verified successfully.",
            password_session_token=password_session_token
        )

    # ── Notification Mutations ────────────────────────────────────────────

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def mark_notification_as_read(self, info: Info, notification_id: int) -> NotificationSuccessType:
        """Mark a single notification as read."""
        from api.models import Notification

        user = info.context.request.user
        try:
            notification = Notification.objects.get(id=notification_id, recipient=user)
            notification.is_read = True
            notification.save(update_fields=["is_read"])
            return NotificationSuccessType(success=True, message="Notification marked as read.")
        except Notification.DoesNotExist:
            raise GraphQLError("Notification not found.")

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def mark_all_notifications_as_read(self, info: Info) -> NotificationSuccessType:
        """Mark all user's notifications as read."""
        from api.models import Notification

        user = info.context.request.user
        count = Notification.objects.filter(recipient=user, is_read=False).update(is_read=True)
        return NotificationSuccessType(
            success=True,
            message=f"{count} notification(s) marked as read."
        )

    # ─── Settings Access OTP ─────────────────────────────────────────────────────
    # One OTP unlocks the whole Settings area for a fixed 10 minutes. The grant is
    # stored server-side (SettingsAccess) and bound to the access token it was
    # issued for. Protected Settings APIs check it with @require_settings_verified.

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_role(["business"])
    def request_settings_otp(self, info: Info) -> RequestPaymentOtpResponse:
        """Sends the Settings OTP. Called only when the user clicks 'Verify & Continue'."""
        try:
            user = info.context.request.user
            generate_and_send_otp(user, purpose="Settings Access")
            return RequestPaymentOtpResponse(success=True, message="OTP sent to your registered email.")
        except Exception as e:
            raise GraphQLError(str(e))

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_role(["business"])
    def resend_settings_otp(self, info: Info) -> RequestPaymentOtpResponse:
        """Resends the Settings OTP (same resend interval / cooldown; attempt counter is not reset)."""
        try:
            user = info.context.request.user
            generate_and_send_otp(user, purpose="Settings Access")
            return RequestPaymentOtpResponse(success=True, message="A new OTP has been sent to your email.")
        except Exception as e:
            raise GraphQLError(str(e))

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_role(["business"])
    def verify_settings_otp(self, info: Info, otp_code: str) -> VerifySettingsOtpResponse:
        """
        Verifies the Settings OTP.

        - Correct OTP: opens a Settings session for exactly SettingsAccess.SESSION_MINUTES
          from now, bound to this access token. It is never extended.
        - Wrong / expired OTP: increments the attempt counter.
        - 3rd wrong attempt: security email + all refresh tokens blacklisted (same as
          the payment / password OTP flows), message LOCKED_OUT.
        """
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken

        MAX_SETTINGS_OTP_ATTEMPTS = 3

        user = info.context.request.user
        access, _ = SettingsAccess.objects.get_or_create(user=user)

        if access.attempt_count >= MAX_SETTINGS_OTP_ATTEMPTS:
            return VerifySettingsOtpResponse(success=False, message="LOCKED_OUT")

        otp_record = OTP.objects.filter(user=user, otp_code=otp_code).first()

        if not otp_record or otp_record.is_expired():
            access.attempt_count += 1
            access.save()
            remaining = MAX_SETTINGS_OTP_ATTEMPTS - access.attempt_count

            if remaining > 0:
                return VerifySettingsOtpResponse(
                    success=False,
                    message=f"Invalid OTP. {remaining} attempt(s) remaining."
                )

            access.locked_at = timezone.now()
            access.verified_until = None
            access.verified_jti = ""
            access.save()

            full_name = f"{user.first_name} {user.last_name}"
            try:
                SESEmailSender().send_email(
                    recipient=user.email,
                    subject="Security Alert: Suspicious Activity on Your Account",
                    body_text=(
                        f"Hello {full_name},\n\n"
                        f"We detected 3 failed attempts to unlock your account settings.\n"
                        f"If this was not you, your account may be compromised.\n\n"
                        f"Please reset your password immediately via account recovery:\n"
                        f"Go to the login page and click 'Forgot Password'.\n\n"
                        f"If this was you, you can ignore this email.\n\n"
                        f"The TWAM Team"
                    ),
                    body_html=f"""
                    <html><body>
                        <p>Hello {full_name},</p>
                        <p>We detected <strong>3 failed attempts</strong> to unlock your account settings.</p>
                        <p>If this was <strong>not you</strong>, your account may be compromised.</p>
                        <p><strong>Please reset your password immediately via account recovery.</strong><br>
                        Go to the login page and click <em>Forgot Password</em>.</p>
                        <p>If this was you, you can ignore this email.</p>
                        <br>
                        <p>The TWAM Team</p>
                    </body></html>
                    """,
                )
            except Exception as mail_err:
                logger.error(f"Failed to send security email for user {user.email}: {mail_err}")

            try:
                for token in OutstandingToken.objects.filter(user=user):
                    BlacklistedToken.objects.get_or_create(token=token)
            except Exception as blacklist_err:
                logger.error(f"Failed to blacklist tokens for user {user.email}: {blacklist_err}")

            return VerifySettingsOtpResponse(success=False, message="LOCKED_OUT")

        # Correct OTP: fixed window from this moment
        otp_record.delete()
        verified_at = timezone.now()
        access.attempt_count = 0
        access.locked_at = None
        access.verified_until = verified_at + timedelta(minutes=SettingsAccess.SESSION_MINUTES)
        access.verified_jti = get_access_token_jti(info) or ""
        access.save()

        return VerifySettingsOtpResponse(
            success=True,
            message="OTP verified successfully.",
            expires_at=access.verified_until,
            seconds_remaining=SettingsAccess.SESSION_MINUTES * 60,
        )

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    def lock_settings_session(self, info: Info) -> bool:
        """Ends the Settings session immediately (used on logout)."""
        access = SettingsAccess.objects.filter(user=info.context.request.user).first()
        if access:
            access.clear_session()
        return True

    # ── Business Deactivation ─────────────────────────────────────────────

    @strawberry.mutation
    @require_api_secret
    @require_authentication
    @jwt_required
    @require_role(["business"])
    @require_settings_verified
    def deactivate_business_account(self, info: Info) -> DeactivateBusinessResponse:
        """
        Permanently deactivate a business account.
        Blocked if there are any outstanding (pending/grace/failed) invoices.
        On success: phases out ads, disables listings, marks profile as
        deactivated, disables the user account, blacklists all JWT tokens
        (immediate session revocation), and notifies admins.
        """
        from api.models import MonthlyInvoice, Advertisement, Listings, AdvertisementChannel, Notification
        from api.types import DeactivateBusinessResponse
        from api.notification_service import create_notification
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken

        user = info.context.request.user
        business_profile = user.business_profile

        # 1. Check for outstanding financial obligations
        outstanding = MonthlyInvoice.objects.filter(
            user=user,
            status__in=["pending", "grace", "failed"]
        ).exists()

        if outstanding:
            return DeactivateBusinessResponse(
                success=False,
                message="Cannot deactivate: you have outstanding bills. Please settle all dues first."
            )

        # 2. Phase-out all active ads
        Advertisement.objects.filter(
            user=user,
            advertisement_status="active"
        ).update(advertisement_status="phaseout")

        # 3. Deactivate all active listings
        Listings.objects.filter(
            user=user,
            listing_status="active"
        ).update(listing_status="disabled")

        # 4. Mark business profile as deactivated
        business_profile.is_deactivated = True
        business_profile.deactivated_at = timezone.now()
        business_profile.save(update_fields=["is_deactivated", "deactivated_at"])

        # 5. Disable user account
        user.is_active = False
        user.save(update_fields=["is_active"])

        # 6. Blacklist all JWT tokens to revoke every active session immediately
        try:
            outstanding_tokens = OutstandingToken.objects.filter(user=user)
            for token in outstanding_tokens:
                BlacklistedToken.objects.get_or_create(token=token)
        except Exception as blacklist_err:
            logger.error(f"Failed to blacklist tokens during deactivation for {user.email}: {blacklist_err}")

        # 7. Notify admins
        admins = User.objects.filter(role="admin", is_active=True)
        for admin in admins:
            create_notification(
                recipient=admin,
                notification_type="business_deactivated",
                title="Business Account Deactivated",
                message=f"{business_profile.business_name} ({user.email}) has deactivated their account.",
                recipient_role="admin",
            )

        logger.info(f"Business account deactivated: {user.email}")

        return DeactivateBusinessResponse(
            success=True,
            message="Your account has been successfully deactivated. To regain access, you will need to register again."
        )

