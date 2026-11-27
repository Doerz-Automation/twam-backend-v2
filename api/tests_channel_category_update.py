"""Admins can change a running channel's categories, subcategories and other
details without touching its discount; discount date rules still apply when the
discount itself is created or changed."""
import json
from datetime import date, timedelta
from decimal import Decimal

from django.test import Client, TestCase, override_settings

from api.models import (AdvertisementCategory, AdvertisementChannel, BusinessCategory,
                        ChannelPriceHistory, User)
from api.utils import API_SECRET_KEY, generate_jwt_token

UPDATE = """
mutation Update($input: UpdateAdvertisementChannelInput!) {
  updateAdvertisementChannel(input: $input) { id message }
}
"""


@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class CategoryUpdateOnDiscountedChannelTests(TestCase):

    def setUp(self):
        self.admin = User.objects.create_user(email="admin-cat@example.com", password="pw")
        self.admin.role, self.admin.is_active = "admin", True
        self.admin.save()
        self.today = date.today()
        self.food = BusinessCategory.objects.create(name="Food")
        self.new_cat = BusinessCategory.objects.create(name="Brand New Category")

    def channel(self, discount_start_offset, price="9.99", discount="4.99"):
        """A running channel with a discount starting `discount_start_offset` days from today."""
        ch = AdvertisementChannel.objects.create(
            channel_name="Running", city="Toronto", province="Ontario", locality="Downtown",
            latitude=43.65, longitude=-79.38, price_per_day=Decimal(price),
            start_date=self.today - timedelta(days=10), end_date=self.today + timedelta(days=30),
            status="active", timezone="America/Toronto",
            apply_discount=True, discount_price=Decimal(discount),
            discount_start_date=self.today + timedelta(days=discount_start_offset),
            discount_end_date=self.today + timedelta(days=20),
            sub_business_categories=["Pizza"],
        )
        AdvertisementCategory.objects.create(advertisement=ch, category=self.food)
        return ch

    def page_payload(self, ch, **changes):
        """What the edit page sends: everything, with the saved discount untouched."""
        payload = {
            "id": ch.id, "channelName": ch.channel_name, "city": ch.city, "province": ch.province,
            "locality": ch.locality, "latitude": ch.latitude, "longitude": ch.longitude,
            "pricePerDay": float(ch.price_per_day),
            "businessCategory": [self.food.id], "subBusinessCategories": list(ch.sub_business_categories),
            "applyDiscount": True, "discountPrice": float(ch.discount_price or 0),
            "discountStartDate": str(ch.discount_start_date) if ch.discount_start_date else None,
            "discountEndDate": str(ch.discount_end_date) if ch.discount_end_date else None,
        }
        payload.update(changes)
        return payload

    def update(self, payload):
        token = generate_jwt_token(self.admin)["access"]
        return Client().post(
            "/graphql",
            data=json.dumps({"query": UPDATE, "variables": {"input": payload}}),
            content_type="application/json",
            HTTP_X_API_SECRET=API_SECRET_KEY,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()

    def assertDiscountUnchanged(self, before):
        after = AdvertisementChannel.objects.get(id=before.id)
        self.assertEqual(
            (after.apply_discount, after.discount_price, after.discount_start_date, after.discount_end_date),
            (before.apply_discount, before.discount_price, before.discount_start_date, before.discount_end_date),
        )
        self.assertFalse(after.pending_apply_discount)
        self.assertIsNone(after.pending_discount_effective_at)

    def categories(self, ch):
        return set(AdvertisementCategory.objects.filter(advertisement=ch).values_list("category__name", flat=True))

    # --- unrelated changes keep the discount ------------------------------------

    def test_add_category_to_channel_with_active_discount(self):
        ch = self.channel(discount_start_offset=-3)  # started 3 days ago
        result = self.update(self.page_payload(ch, businessCategory=[self.food.id, self.new_cat.id]))
        self.assertNotIn("errors", result, result)
        self.assertEqual(self.categories(ch), {"Food", "Brand New Category"})
        self.assertDiscountUnchanged(ch)

    def test_add_subcategory_to_channel_whose_discount_starts_tomorrow(self):
        ch = self.channel(discount_start_offset=1)  # locked: starts tomorrow
        result = self.update(self.page_payload(ch, subBusinessCategories=["Pizza", "Brand New Sub"]))
        self.assertNotIn("errors", result, result)
        ch_after = AdvertisementChannel.objects.get(id=ch.id)
        self.assertEqual(ch_after.sub_business_categories, ["Pizza", "Brand New Sub"])
        self.assertDiscountUnchanged(ch)

    def test_add_category_to_channel_whose_discount_starts_today(self):
        ch = self.channel(discount_start_offset=0)
        result = self.update(self.page_payload(ch, businessCategory=[self.food.id, self.new_cat.id]))
        self.assertNotIn("errors", result, result)
        self.assertDiscountUnchanged(ch)

    def test_remove_category_and_subcategory(self):
        ch = self.channel(discount_start_offset=-3)
        result = self.update(self.page_payload(ch, businessCategory=[self.new_cat.id], subBusinessCategories=[]))
        self.assertNotIn("errors", result, result)
        self.assertEqual(self.categories(ch), {"Brand New Category"})
        self.assertEqual(AdvertisementChannel.objects.get(id=ch.id).sub_business_categories, [])
        self.assertDiscountUnchanged(ch)

    def test_unrelated_details_keep_discount(self):
        ch = self.channel(discount_start_offset=-3)
        result = self.update(self.page_payload(ch, locality="Midtown", channelName="Renamed"))
        self.assertNotIn("errors", result, result)
        self.assertEqual(AdvertisementChannel.objects.get(id=ch.id).locality, "Midtown")
        self.assertDiscountUnchanged(ch)

    def test_unchanged_price_does_not_open_a_new_price_period(self):
        ch = self.channel(discount_start_offset=-3, price="9.99")
        before = ChannelPriceHistory.objects.filter(channel=ch).count()
        result = self.update(self.page_payload(ch, locality="Midtown"))
        self.assertNotIn("errors", result, result)
        self.assertEqual(ChannelPriceHistory.objects.filter(channel=ch).count(), before)
        self.assertEqual(AdvertisementChannel.objects.get(id=ch.id).price_per_day, Decimal("9.99"))

    # --- discount rules still apply when the discount is changed -------------------

    def test_moving_a_future_discount_to_today_is_rejected(self):
        ch = self.channel(discount_start_offset=5)
        result = self.update(self.page_payload(ch, discountStartDate=str(self.today)))
        self.assertIn("at least the next day", json.dumps(result), result)
        self.assertDiscountUnchanged(ch)

    def test_changing_an_active_discount_start_date_is_rejected(self):
        ch = self.channel(discount_start_offset=-3)
        result = self.update(self.page_payload(ch, discountStartDate=str(self.today - timedelta(days=1))))
        self.assertIn("errors", result, result)
        self.assertDiscountUnchanged(ch)

    def test_changing_an_active_discount_price_is_still_locked(self):
        ch = self.channel(discount_start_offset=-3)
        result = self.update(self.page_payload(ch, discountPrice=3.5))
        self.assertIn("Cannot modify discount settings", json.dumps(result), result)
        self.assertDiscountUnchanged(ch)

    def test_new_discount_on_channel_without_one_still_needs_tomorrow(self):
        ch = self.channel(discount_start_offset=5)
        AdvertisementChannel.objects.filter(id=ch.id).update(
            apply_discount=False, discount_price=None, discount_start_date=None, discount_end_date=None)
        ch.refresh_from_db()
        result = self.update(self.page_payload(
            ch, applyDiscount=True, discountPrice=2, discountStartDate=str(self.today), discountEndDate=None))
        self.assertIn("at least the next day", json.dumps(result), result)
