"""A code only proves control of the address it was emailed to."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from api.models import OTP, User
from api.utils import API_SECRET_KEY, find_otp, generate_and_send_otp, generate_jwt_token

CHANGE_EMAIL = """mutation($email: String!) { changeEmail(email: $email) }"""
CONFIRM_EMAIL = """mutation($code: String!, $email: String!) {
  verifyOtpForNewEmail(otpCode: $code, newEmail: $email) }"""
VERIFY_OTP = """mutation($email: String!, $code: String!) {
  verifyOtp(email: $email, otpCode: $code) { id } }"""


@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class OtpBindingTests(TestCase):

    def setUp(self):
        cache.clear()
        patcher = patch("api.utils.send_otp_email")  # never send real emails
        self.sent = patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(email="me@example.com", password="pw")
        self.user.role, self.user.is_active, self.user.is_verified = "user", True, True
        self.user.is_admin_approved = True
        self.user.save()
        self.token = generate_jwt_token(self.user)["access"]

    def gql(self, query, variables, auth=True):
        headers = {"HTTP_X_API_SECRET": API_SECRET_KEY}
        if auth:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {self.token}"
        return Client().post(
            "/graphql",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
            **headers,
        ).json()

    def current_code(self):
        return OTP.objects.get(user=self.user).otp_code

    def email_now(self):
        self.user.refresh_from_db()
        return self.user.email

    # --- email change ---------------------------------------------------------

    def test_code_from_the_new_address_changes_the_email(self):
        self.assertNotIn("errors", self.gql(CHANGE_EMAIL, {"email": "New@Example.com"}))
        self.assertEqual(self.sent.call_args[0][0], "new@example.com")  # mailed to the new address
        result = self.gql(CONFIRM_EMAIL, {"code": self.current_code(), "email": " NEW@example.com"})
        self.assertNotIn("errors", result, result)
        self.assertEqual(self.email_now(), "new@example.com")

    def test_code_sent_to_one_address_cannot_confirm_another(self):
        self.gql(CHANGE_EMAIL, {"email": "mine@example.com"})
        result = self.gql(CONFIRM_EMAIL, {"code": self.current_code(), "email": "victim@example.com"})
        self.assertIn("Invalid OTP", json.dumps(result), result)
        self.assertEqual(self.email_now(), "me@example.com")

    def test_code_sent_to_the_current_email_cannot_confirm_a_change(self):
        generate_and_send_otp(self.user, purpose="Settings Access")  # mailed to me@
        result = self.gql(CONFIRM_EMAIL, {"code": self.current_code(), "email": "other@example.com"})
        self.assertIn("Invalid OTP", json.dumps(result), result)
        self.assertEqual(self.email_now(), "me@example.com")

    def test_email_taken_after_the_code_was_sent_is_refused(self):
        self.gql(CHANGE_EMAIL, {"email": "new@example.com"})
        User.objects.create_user(email="new@example.com", password="pw")
        result = self.gql(CONFIRM_EMAIL, {"code": self.current_code(), "email": "new@example.com"})
        self.assertIn("Email already exists", json.dumps(result), result)
        self.assertEqual(self.email_now(), "me@example.com")

    # --- account verification -------------------------------------------------

    def test_email_change_code_cannot_verify_the_account(self):
        self.gql(CHANGE_EMAIL, {"email": "new@example.com"})
        result = self.gql(VERIFY_OTP, {"email": "me@example.com", "code": self.current_code()}, auth=False)
        self.assertIn("Invalid OTP", json.dumps(result), result)

    def test_account_code_still_verifies_the_account(self):
        generate_and_send_otp(self.user, purpose="Signup")
        self.assertEqual(self.sent.call_args[0][0], "me@example.com")
        result = self.gql(VERIFY_OTP, {"email": "me@example.com", "code": self.current_code()}, auth=False)
        self.assertNotIn("errors", result, result)

    # --- find_otp -------------------------------------------------------------

    def test_codes_issued_before_the_field_existed_still_work_for_the_account(self):
        OTP.objects.create(user=self.user, otp_code="123456", sent_to=None,
                           expires_at=timezone.now() + timedelta(minutes=5))
        self.assertIsNotNone(find_otp(self.user, "123456"))
        # ...but never for an email change
        self.assertIsNone(find_otp(self.user, "123456", new_email="x@example.com"))

    def test_wrong_code_is_not_found(self):
        generate_and_send_otp(self.user, purpose="Signup")
        self.assertIsNone(find_otp(self.user, "000000" if self.current_code() != "000000" else "111111"))
