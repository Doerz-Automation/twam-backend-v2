import json
from datetime import date, timedelta

from django.test import Client, TestCase, override_settings

from api.models import Advertisement, BusinessProfile, User
from api.utils import API_SECRET_KEY, generate_jwt_token

UPDATE_AD = """
mutation($input: UpdateAdvertisementInput!) { updateAdvertisement(input: $input) { id message } }
"""

START_DATE_ERROR = "Cannot change the start date of an advertisement that has already started"


# The middleware checks settings.API_SECRET_KEY and the resolvers check the value
# api.utils imported at startup; under the test settings those differ, so align them.
@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class UpdateRunningAdvertisementTests(TestCase):
    """A business can edit an ad that is already running, as long as the start date is untouched."""

    def setUp(self):
        self.business = User.objects.create_user(email="runner@example.com", password="password")
        self.business.role = "business"
        self.business.is_active = True
        self.business.is_admin_approved = True
        self.business.save()
        BusinessProfile.objects.create(user=self.business, business_name="Running Business")

        today = date.today()
        self.start = today - timedelta(days=3)  # already started
        self.ad = Advertisement.objects.create(
            user=self.business,
            advertisment_name="Original title",
            advertisment_description="Original description",
            advertisement_image=["https://example.com/a.png"],
            start_date=self.start,
            end_date=today + timedelta(days=10),
            advertisment_cost=50,
            advertisement_status="active",
        )

    def call(self, **fields):
        token = generate_jwt_token(self.business)["access"]
        variables = {"input": {"id": self.ad.id, **fields}}
        return Client().post(
            "/graphql",
            data=json.dumps({"query": UPDATE_AD, "variables": variables}),
            content_type="application/json",
            HTTP_X_API_SECRET=API_SECRET_KEY,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()

    def test_editing_title_with_unchanged_start_date_works(self):
        # The edit form always sends the current start date back with the change.
        result = self.call(advertismentName="New title", startDate=str(self.start))
        self.assertNotIn("errors", result, result)
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.advertisment_name, "New title")

    def test_editing_description_images_and_end_date_works(self):
        new_end = date.today() + timedelta(days=20)
        result = self.call(
            advertismentDescription="New description",
            advertisementImage=["https://example.com/b.png", "https://example.com/c.png"],
            startDate=str(self.start),
            endDate=str(new_end),
        )
        self.assertNotIn("errors", result, result)
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.advertisment_description, "New description")
        self.assertEqual(len(self.ad.advertisement_image), 2)
        self.assertEqual(self.ad.end_date, new_end)

    def test_changing_the_start_date_of_a_running_ad_is_still_refused(self):
        result = self.call(advertismentName="Hacked", startDate=str(self.start + timedelta(days=1)))
        self.assertIn(START_DATE_ERROR, json.dumps(result), result)
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.advertisment_name, "Original title")
        self.assertEqual(self.ad.start_date, self.start)

    def test_more_than_three_images_is_refused(self):
        result = self.call(advertisementImage=[f"https://example.com/{i}.png" for i in range(4)])
        self.assertIn("up to 3 images", json.dumps(result), result)
