from types import SimpleNamespace
from unittest.mock import patch

import stripe
from django.test import TestCase

from api import stripe_services
from api.models import User


@patch.object(stripe_services.stripe, "api_key", "sk_test_dummy")
class StripeCustomerLinkingTests(TestCase):
    """A new account must never inherit a Stripe customer (and its saved cards)
    just because an older account used the same email address."""

    def setUp(self):
        self.user = User.objects.create_user(email="again@example.com", password="password")

    def test_a_customer_with_the_same_email_is_not_adopted(self):
        old_customer = SimpleNamespace(id="cus_OLD", data=None)
        with patch.object(stripe_services.stripe.Customer, "list", return_value=SimpleNamespace(data=[old_customer])) as list_call, \
             patch.object(stripe_services.stripe.Customer, "create", return_value=SimpleNamespace(id="cus_NEW")) as create_call:
            customer_id = stripe_services.create_stripe_customer(self.user)

        self.assertEqual(customer_id, "cus_NEW")
        list_call.assert_not_called()  # the email is never used to look a customer up
        create_call.assert_called_once()
        self.user.refresh_from_db()
        self.assertEqual(self.user.stripe_customer_id, "cus_NEW")

    def test_the_customer_already_linked_to_the_account_is_reused(self):
        self.user.stripe_customer_id = "cus_MINE"
        self.user.save()
        with patch.object(stripe_services.stripe.Customer, "retrieve", return_value=SimpleNamespace(id="cus_MINE")), \
             patch.object(stripe_services.stripe.Customer, "create") as create_call:
            customer_id = stripe_services.create_stripe_customer(self.user)

        self.assertEqual(customer_id, "cus_MINE")
        create_call.assert_not_called()

    def test_a_linked_customer_that_was_deleted_in_stripe_is_replaced(self):
        self.user.stripe_customer_id = "cus_GONE"
        self.user.save()
        gone = stripe.error.InvalidRequestError("No such customer", param="id")
        with patch.object(stripe_services.stripe.Customer, "retrieve", side_effect=gone), \
             patch.object(stripe_services.stripe.Customer, "create", return_value=SimpleNamespace(id="cus_FRESH")):
            customer_id = stripe_services.create_stripe_customer(self.user)

        self.assertEqual(customer_id, "cus_FRESH")
        self.user.refresh_from_db()
        self.assertEqual(self.user.stripe_customer_id, "cus_FRESH")

    def test_a_linked_customer_marked_deleted_is_replaced(self):
        self.user.stripe_customer_id = "cus_DELETED"
        self.user.save()
        with patch.object(stripe_services.stripe.Customer, "retrieve", return_value=SimpleNamespace(id="cus_DELETED", deleted=True)), \
             patch.object(stripe_services.stripe.Customer, "create", return_value=SimpleNamespace(id="cus_FRESH")):
            customer_id = stripe_services.create_stripe_customer(self.user)

        self.assertEqual(customer_id, "cus_FRESH")
