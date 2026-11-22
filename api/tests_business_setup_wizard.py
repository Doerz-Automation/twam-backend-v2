import json
from datetime import time

from django.test import Client, TestCase, override_settings

from api.decorators import SETTINGS_VERIFICATION_REQUIRED
from api.models import (
    BusinessCategory,
    BusinessHours,
    BusinessProfile,
    Listings,
    SettingsAccess,
    User,
)
from api.utils import API_SECRET_KEY, generate_jwt_token

UPDATE_PROFILE = """
mutation($input: BusinessProfileUpdateInput!) {
  updateBusinessProfile(input: $input) { businessName category subcategories }
}
"""


# The middleware checks settings.API_SECRET_KEY and the resolvers check the value
# api.utils imported at startup; under the test settings those differ, so align them.
# DISABLE_SETTINGS_OTP is off, as on a real server.
@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY, DISABLE_SETTINGS_OTP=False)
class BusinessSetupWizardTests(TestCase):
    """An approved business must be able to finish the setup wizard.

    Older signups already hold a category and subcategories, so the profile becomes
    "complete" after the Location and Business Info steps, before the Business
    Category step is saved. That last save must not demand a Settings code."""

    def setUp(self):
        self.category = BusinessCategory.objects.create(name="Dining")
        self.user = User.objects.create_user(email="wizard@example.com", password="password")
        self.user.role = "business"
        self.user.is_active = True
        self.user.is_admin_approved = True
        self.user.save()
        # Every field the backend counts as "complete", category and subcategories included.
        self.profile = BusinessProfile.objects.create(
            user=self.user, business_name="Wizard Cafe", description="Good food",
            logo_url="https://example.com/logo.png", address="1 Main St", city="Toronto",
            state="Ontario", zip_code="M5V1A1", latitude=43.65, longitude=-79.38,
            phone="+14165550100", payment_methods=["Visa"],
            category=self.category, subcategories=["Asian"],
        )
        BusinessHours.objects.create(
            business=self.profile, day="monday", opening_time=time(9, 0),
            closing_time=time(17, 0), is_closed=False,
        )

    def save_category(self):
        token = generate_jwt_token(self.user)["access"]
        variables = {"input": {"categoryId": str(self.category.id), "subcategories": ["Asian", "Buffet"]}}
        return Client().post(
            "/graphql",
            data=json.dumps({"query": UPDATE_PROFILE, "variables": variables}),
            content_type="application/json",
            HTTP_X_API_SECRET=API_SECRET_KEY,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()

    def test_finishing_the_category_step_works_while_setup_is_unfinished(self):
        result = self.save_category()
        self.assertNotIn("errors", result, result)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.subcategories, ["Asian", "Buffet"])

    def test_profile_edits_still_need_a_settings_code_once_the_business_has_a_free_daily_ad(self):
        Listings.objects.create(user=self.user, listing_name="Lunch special")
        result = self.save_category()
        self.assertIn(SETTINGS_VERIFICATION_REQUIRED, json.dumps(result), result)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.subcategories, ["Asian"])

    def test_profile_edits_still_need_a_settings_code_once_the_business_has_opened_settings(self):
        SettingsAccess.objects.create(user=self.user)
        result = self.save_category()
        self.assertIn(SETTINGS_VERIFICATION_REQUIRED, json.dumps(result), result)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.subcategories, ["Asian"])

    def test_an_incomplete_profile_can_always_be_edited(self):
        self.profile.phone = ""
        self.profile.save()
        Listings.objects.create(user=self.user, listing_name="Lunch special")
        result = self.save_category()
        self.assertNotIn("errors", result, result)
