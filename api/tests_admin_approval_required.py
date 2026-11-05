import json
from datetime import date, timedelta

from django.test import Client, TestCase, override_settings

from api.decorators import ADMIN_APPROVAL_REQUIRED_MESSAGE
from api.models import Advertisement, BusinessProfile, Listings, User
from api.utils import API_SECRET_KEY, generate_jwt_token

CREATE_AD = """
mutation($input: AdvertisementInput!) { createAdvertisement(input: $input) { id message } }
"""
CREATE_LISTING = """
mutation($input: CreateListingInput!) { createListings(input: $input) { id message } }
"""


# The middleware checks settings.API_SECRET_KEY and the resolvers check the value
# api.utils imported at startup; under the test settings those differ, so align them.
@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class AdminApprovalRequiredTests(TestCase):
    """A business that an admin has not approved cannot create ads or free daily ads."""

    def setUp(self):
        self.business = User.objects.create_user(email="unapproved@example.com", password="password")
        self.business.role = "business"
        self.business.is_active = True
        self.business.is_admin_approved = False
        self.business.save()
        BusinessProfile.objects.create(user=self.business, business_name="Waiting Business")

    def call(self, query, variables):
        token = generate_jwt_token(self.business)["access"]
        return Client().post(
            "/graphql",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
            HTTP_X_API_SECRET=API_SECRET_KEY,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()

    def ad_input(self):
        today = date.today()
        return {
            "advertismentName": "Sneaky ad", "advertismentDescription": "should not be created",
            "advertisementImage": ["https://example.com/a.png"], "channelIds": [1], "advertismentCost": 100,
            "startDate": str(today + timedelta(days=1)), "endDate": str(today + timedelta(days=5)),
        }

    def test_unapproved_business_cannot_create_an_advertisement(self):
        result = self.call(CREATE_AD, {"input": self.ad_input()})
        self.assertIn(ADMIN_APPROVAL_REQUIRED_MESSAGE, json.dumps(result), result)
        self.assertEqual(Advertisement.objects.filter(user=self.business).count(), 0)

    def test_unapproved_business_cannot_create_a_free_daily_ad(self):
        result = self.call(CREATE_LISTING, {"input": {"listingName": "Sneaky listing", "listingDescription": "x", "listingImage": []}})
        self.assertIn(ADMIN_APPROVAL_REQUIRED_MESSAGE, json.dumps(result), result)
        self.assertEqual(Listings.objects.filter(user=self.business).count(), 0)

    def test_approved_business_gets_past_the_approval_check(self):
        self.business.is_admin_approved = True
        self.business.save()
        result = self.call(CREATE_LISTING, {"input": {"listingName": "Real listing", "listingDescription": "x", "listingImage": []}})
        self.assertNotIn(ADMIN_APPROVAL_REQUIRED_MESSAGE, json.dumps(result), result)
