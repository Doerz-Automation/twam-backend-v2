"""An unverified account that signs in is always sent on to the OTP page.
The OTP throttles limit how many codes are emailed; they never block that step."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from api.models import OTP, User
from api.utils import API_SECRET_KEY

LOGIN = """mutation($email: String!, $password: String!) {
  login(input: {email: $email, password: $password}) { id } }"""
RESEND = """mutation($email: String!) { resendOtp(email: $email) { __typename } }"""
NOT_VERIFIED = "User has not completed OTP verification."


@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class UnverifiedLoginTests(TestCase):

    def setUp(self):
        cache.clear()
        patcher = patch("api.utils.send_otp_email")  # never send real emails
        self.sent = patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(email="new@example.com", password="pw")
        self.user.role, self.user.is_verified = "business", False
        self.user.save()

    def gql(self, query, variables):
        return Client().post(
            "/graphql",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
            HTTP_X_API_SECRET=API_SECRET_KEY,
        ).json()

    def login(self, email="new@example.com", password="pw"):
        return json.dumps(self.gql(LOGIN, {"email": email, "password": password}).get("errors"))

    def test_signing_in_twice_quickly_still_goes_to_otp_and_sends_one_email(self):
        self.assertIn(NOT_VERIFIED, self.login())
        # within the resend interval: no second email, but still sent to OTP
        result = self.login()
        self.assertIn(NOT_VERIFIED, result)
        self.assertNotIn("Please wait", result)
        self.assertEqual(self.sent.call_count, 1)

    def test_resend_cooldown_does_not_block_signing_in(self):
        OTP.objects.create(user=self.user, otp_code="123456", sent_to=self.user.email,
                           retry_count=99, last_sent_at=timezone.now(),
                           expires_at=timezone.now() + timedelta(minutes=5))
        result = self.login()
        self.assertIn(NOT_VERIFIED, result)
        self.assertNotIn("Too many OTP requests", result)
        self.sent.assert_not_called()

    def test_explicit_resend_is_still_throttled(self):
        self.login()
        result = json.dumps(self.gql(RESEND, {"email": "new@example.com"}))
        self.assertIn("Please wait", result)
        self.assertEqual(self.sent.call_count, 1)

    def test_not_verified_is_not_counted_as_a_failed_login(self):
        for _ in range(8):  # the per-account limit is 5
            result = self.login()
            self.assertIn(NOT_VERIFIED, result)
            self.assertNotIn("Too many attempts", result)

    def test_wrong_password_does_not_reach_otp_or_send_a_code(self):
        result = self.login(password="wrong")
        self.assertIn("Invalid email or password", result)
        self.assertNotIn(NOT_VERIFIED, result)
        self.sent.assert_not_called()

    def test_wrong_password_on_unverified_account_counts_as_a_failure(self):
        for _ in range(5):
            self.login(password="wrong")
        self.assertIn("Too many attempts", self.login())

    def test_wrong_password_does_not_reveal_a_deactivated_or_inactive_account(self):
        self.user.is_verified, self.user.is_active = True, False
        self.user.save()
        result = self.login(password="wrong")
        self.assertIn("Invalid email or password", result)
        self.assertNotIn("inactive", result)

    def test_real_failures_are_still_counted(self):
        verified = User.objects.create_user(email="ok@example.com", password="right")
        verified.role, verified.is_verified, verified.is_active = "business", True, True
        verified.save()
        for _ in range(5):
            self.login(email="ok@example.com", password="wrong")
        self.assertIn("Too many attempts", self.login(email="ok@example.com", password="right"))
