from unittest.mock import patch

from django.test import TestCase

from api.models import BusinessProfile, Notification, User
from api.notification_service import (
    PAYMENT_SETUP_COMPLETE_MESSAGE,
    create_notification,
    notify_payment_setup_complete,
)

CARD = [{"id": "pm_test_1"}]
TARGET = "api.stripe_services.get_user_payment_methods"


class PaymentSetupNotificationTests(TestCase):
    def setUp(self):
        self.business = User.objects.create_user(email="payment-setup@example.com", password="password")
        self.business.role = "business"
        self.business.is_admin_approved = True
        self.business.save()
        BusinessProfile.objects.create(user=self.business, business_name="Test Business")

    def sent(self, user=None):
        return Notification.objects.filter(
            recipient=user or self.business, notification_type="payment_setup_complete"
        )

    def test_sent_when_approved_and_card_on_file(self):
        with patch(TARGET, return_value=CARD):
            self.assertTrue(notify_payment_setup_complete(self.business))
        notification = self.sent().get()
        self.assertEqual(notification.message, PAYMENT_SETUP_COMPLETE_MESSAGE)
        self.assertEqual(notification.recipient_role, "business")

    def test_not_sent_before_admin_approval(self):
        self.business.is_admin_approved = False
        self.business.save()
        with patch(TARGET, return_value=CARD):
            self.assertFalse(notify_payment_setup_complete(self.business))
        self.assertFalse(self.sent().exists())

    def test_not_sent_without_a_card(self):
        with patch(TARGET, return_value=[]):
            self.assertFalse(notify_payment_setup_complete(self.business))
        self.assertFalse(self.sent().exists())

    def test_only_sent_once(self):
        with patch(TARGET, return_value=CARD):
            self.assertTrue(notify_payment_setup_complete(self.business))
            self.assertFalse(notify_payment_setup_complete(self.business))
        self.assertEqual(self.sent().count(), 1)

    def test_not_sent_to_other_roles(self):
        consumer = User.objects.create_user(email="consumer@example.com", password="password")
        consumer.role = "user"
        consumer.is_admin_approved = True
        consumer.save()
        with patch(TARGET, return_value=CARD):
            self.assertFalse(notify_payment_setup_complete(consumer))

    def test_separate_from_the_approval_notification(self):
        create_notification(self.business, "business_approved", "Account Approved", "Approved.")
        with patch(TARGET, return_value=CARD):
            notify_payment_setup_complete(self.business)
        types = set(
            Notification.objects.filter(recipient=self.business).values_list("notification_type", flat=True)
        )
        self.assertEqual(types, {"business_approved", "payment_setup_complete"})
