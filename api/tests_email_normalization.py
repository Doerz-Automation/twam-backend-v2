"""Emails are normalized in one place (api/email_utils.py) and every
authentication flow goes through it, so an email typed with capitals or spaces
works the same everywhere."""
import importlib
import json
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from api.email_utils import normalize_email
from api.models import OTP, User
from api.utils import API_SECRET_KEY

TYPED = "  Ann.Lee@Example.COM "
STORED = "ann.lee@example.com"

LOGIN = """mutation($email: String!) {
  login(input: {email: $email, password: "Password123!"}) { id accessToken } }"""
FORGOT = """mutation($email: String!) { forgotPassword(email: $email) { success userId } }"""
CLIENT_REGISTER = """mutation($email: String!) {
  clientRegister(input: {email: $email, password: "Password123!", firstName: "A", lastName: "B"}) { id } }"""
REGISTER = """mutation($email: String!) {
  register(input: {email: $email, password: "Password123!", role: "business", businessName: "Cafe"}) { email } }"""
VERIFY_OTP = """mutation($email: String!, $code: String!) {
  verifyOtp(email: $email, otpCode: $code) { id } }"""


class NormalizeEmailTests(TestCase):
    def test_normalize_email(self):
        self.assertEqual(normalize_email(TYPED), STORED)
        self.assertEqual(normalize_email(None), "")
        self.assertEqual(normalize_email("   "), "")


class UserModelTests(TestCase):
    """The model guarantees a normalized stored email, however it is written."""

    def test_create_user_stores_normalized_email(self):
        self.assertEqual(User.objects.create_user(email=TYPED, password="pw").email, STORED)

    def test_create_user_rejects_blank_email(self):
        with self.assertRaises(ValueError):
            User.objects.create_user(email="   ", password="pw")

    def test_save_normalizes_a_changed_email(self):
        # this is how the email-change flow and Django admin write emails
        user = User.objects.create_user(email="old@example.com", password="pw")
        user.email = " New@Example.com"
        user.save()
        user.refresh_from_db()
        self.assertEqual(user.email, "new@example.com")

    def test_get_by_email_and_email_exists_ignore_case_and_spaces(self):
        user = User.objects.create_user(email=STORED, password="pw")
        self.assertEqual(User.objects.get_by_email(TYPED), user)
        self.assertTrue(User.objects.email_exists(TYPED))
        self.assertFalse(User.objects.email_exists(TYPED, exclude_id=user.id))
        with self.assertRaises(User.DoesNotExist):
            User.objects.get_by_email("someone-else@example.com")

    def test_duplicate_with_different_case_is_refused(self):
        User.objects.create_user(email=STORED, password="pw")
        with self.assertRaises(Exception):
            User.objects.create_user(email=TYPED, password="pw")

    def test_django_authentication_ignores_case_and_spaces(self):
        user = User.objects.create_user(email=STORED, password="pw", is_active=True)
        self.assertEqual(authenticate(username=TYPED, password="pw"), user)
        self.assertEqual(User.objects.get_by_natural_key(TYPED), user)


@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class AuthFlowTests(TestCase):
    """Each GraphQL flow accepts the email the way a person typed it."""

    def setUp(self):
        cache.clear()
        patcher = patch("api.utils.send_otp_email")  # never send real emails
        patcher.start()
        self.addCleanup(patcher.stop)

    def gql(self, query, variables):
        return Client().post(
            "/graphql",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
            HTTP_X_API_SECRET=API_SECRET_KEY,
        ).json()

    def make_user(self, **extra):
        user = User.objects.create_user(email=STORED, password="Password123!", **extra)
        user.is_active = user.is_verified = True
        user.role = extra.get("role", "business")
        user.is_admin_approved = True
        user.save()
        return user

    def test_business_signup_then_otp(self):
        result = self.gql(REGISTER, {"email": TYPED})
        self.assertEqual(result["data"]["register"]["email"], STORED, result)
        code = OTP.objects.get(user__email=STORED).otp_code
        self.assertNotIn("errors", self.gql(VERIFY_OTP, {"email": TYPED, "code": code}))

    def test_login(self):
        self.make_user()
        result = self.gql(LOGIN, {"email": TYPED})
        self.assertNotIn("errors", result, result)

    def test_forgot_password(self):
        user = self.make_user()
        result = self.gql(FORGOT, {"email": TYPED})
        self.assertEqual(result["data"]["forgotPassword"]["userId"], user.id, result)

    def test_client_signup_is_stored_normalized_and_blocks_case_duplicates(self):
        self.assertNotIn("errors", self.gql(CLIENT_REGISTER, {"email": TYPED}))
        self.assertTrue(User.objects.filter(email=STORED).exists())
        result = self.gql(CLIENT_REGISTER, {"email": STORED.upper()})
        self.assertIn("Email already exists", json.dumps(result), result)

    def test_business_signup_blocks_case_duplicates(self):
        self.make_user()
        result = self.gql(REGISTER, {"email": STORED.upper()})
        self.assertIn("errors", result)
        self.assertEqual(User.objects.filter(email=STORED).count(), 1)


class NormalizeEmailsMigrationTests(TestCase):
    """0008 fixes stored emails and leaves case-only duplicates for an admin."""

    def run_migration(self):
        module = importlib.import_module("api.migrations.0008_normalize_user_emails")
        module.normalize_emails(django_apps, None)

    def test_mixed_case_emails_are_normalized(self):
        user = User.objects.create_user(email="x@example.com", password="pw")
        User.objects.filter(id=user.id).update(email=" Mixed@Example.com ")  # legacy row
        self.run_migration()
        user.refresh_from_db()
        self.assertEqual(user.email, "mixed@example.com")

    def test_case_only_duplicates_are_left_untouched(self):
        a = User.objects.create_user(email="a1@example.com", password="pw")
        b = User.objects.create_user(email="a2@example.com", password="pw")
        User.objects.filter(id=a.id).update(email="Dup@Example.com")
        User.objects.filter(id=b.id).update(email="dup@example.com")
        self.run_migration()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.email, b.email), ("Dup@Example.com", "dup@example.com"))
