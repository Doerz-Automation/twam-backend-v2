import json
from datetime import date, timedelta

from django.test import Client, TestCase, override_settings

from api.models import AdvertisementChannel, User
from api.utils import API_SECRET_KEY, generate_jwt_token

MUTATION = """
mutation Update($input: UpdateAdvertisementChannelInput!) {
  updateAdvertisementChannel(input: $input) { id message }
}
"""


# The middleware checks settings.API_SECRET_KEY and the resolvers check the value
# api.utils imported at startup; under the test settings those differ, so align them.
@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class DiscountOnRunningChannelTests(TestCase):
    """An admin adds a discount to a channel that is already running."""

    def setUp(self):
        self.admin = User.objects.create_user(email="admin-discount@example.com", password="password")
        self.admin.role = "admin"
        self.admin.is_active = True
        self.admin.save()
        today = date.today()
        self.channel = AdvertisementChannel.objects.create(
            channel_name="Running channel", city="Toronto", province="Ontario", locality="Downtown",
            latitude=43.65, longitude=-79.38, price_per_day=10,
            start_date=today - timedelta(days=10), end_date=today + timedelta(days=30),
            status="active", timezone="America/Toronto",
        )
        self.today = today

    def update(self, **fields):
        token = generate_jwt_token(self.admin)["access"]
        response = Client().post(
            "/graphql",
            data=json.dumps({"query": MUTATION, "variables": {"input": {"id": self.channel.id, **fields}}}),
            content_type="application/json",
            HTTP_X_API_SECRET=API_SECRET_KEY,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()
        self.channel.refresh_from_db()
        return response

    def test_discount_starting_tomorrow_with_end_date_is_saved(self):
        tomorrow, end = self.today + timedelta(days=1), self.today + timedelta(days=11)
        result = self.update(
            applyDiscount=True, discountPrice=20,
            discountStartDate=str(tomorrow), discountEndDate=str(end),
        )
        self.assertNotIn("errors", result, result)
        # A discount that starts after tomorrow is saved on the live fields
        self.assertTrue(self.channel.apply_discount, result)
        self.assertEqual(float(self.channel.discount_price), 20)
        self.assertEqual(self.channel.discount_start_date, tomorrow)
        self.assertEqual(self.channel.discount_end_date, end)

    def test_page_payload_with_unchanged_dates_is_saved(self):
        """The page re-sends price and coordinates; dates are only sent when changed."""
        tomorrow = self.today + timedelta(days=1)
        result = self.update(
            channelName="Running channel", pricePerDay=10, latitude=43.65, longitude=-79.38,
            applyDiscount=True, discountPrice=15,
            discountStartDate=str(tomorrow), discountEndDate=None,
        )
        self.assertNotIn("errors", result, result)
        self.assertTrue(self.channel.apply_discount, result)
        self.assertEqual(self.channel.discount_start_date, tomorrow)

    def test_discount_starting_today_is_rejected_with_a_clear_message(self):
        result = self.update(
            applyDiscount=True, discountPrice=20,
            discountStartDate=str(self.today), discountEndDate=str(self.today + timedelta(days=5)),
        )
        self.assertIn("errors", result, result)
        self.assertIn("at least the next day", result["errors"][0]["message"])
        self.assertFalse(self.channel.apply_discount)
