import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from api.models import OTP, User
from api.utils import API_SECRET_KEY, generate_jwt_token

REGISTER = """mutation($email: String!) {
  register(input: {email: $email, password: "Password123!", role: "business",
                   firstName: "Ann", lastName: "Lee", businessName: "Ann's Cafe"}) { id email } }"""
VERIFY_OTP = """mutation($email: String!, $code: String!) {
  verifyOtp(email: $email, otpCode: $code) { id accessToken isVerified } }"""
RESEND_OTP = """mutation($email: String!) { resendOtp(email: $email) { __typename } }"""
REQUESTS = """query { getUsersList(role: "business", isAdminApproved: false) { email } }"""


@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class SignupOtpTests(TestCase):
    """A business typing its email with capitals or a trailing space can still
    verify it, and only verified sign-ups reach the admin approval list."""

    def setUp(self):
        cache.clear()
        # never send real emails from tests
        patcher = patch("api.utils.send_otp_email")
        patcher.start()
        self.addCleanup(patcher.stop)

    def gql(self, query, variables=None, token=None):
        headers = {"HTTP_X_API_SECRET": API_SECRET_KEY}
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return Client().post(
            "/graphql",
            data=json.dumps({"query": query, "variables": variables or {}}),
            content_type="application/json",
            **headers,
        ).json()

    def code_for(self, email):
        return OTP.objects.filter(user__email=email).latest("id").otp_code

    def test_email_with_capitals_and_space_can_be_verified(self):
        typed = "Ann.Lee@Example.com "
        result = self.gql(REGISTER, {"email": typed})
        self.assertNotIn("errors", result, result)
        self.assertEqual(result["data"]["register"]["email"], "ann.lee@example.com")

        # the OTP page sends the email exactly as it was typed
        result = self.gql(VERIFY_OTP, {"email": typed, "code": self.code_for("ann.lee@example.com")})
        self.assertNotIn("errors", result, result)
        self.assertTrue(result["data"]["verifyOtp"]["isVerified"])
        self.assertTrue(User.objects.get(email="ann.lee@example.com").is_verified)

    def test_resend_otp_works_with_typed_casing(self):
        self.gql(REGISTER, {"email": "ann@example.com"})
        result = self.gql(RESEND_OTP, {"email": "ANN@example.com"})
        self.assertNotIn("User with this email does not exist", json.dumps(result), result)

    def test_unverified_signup_is_not_an_approval_request(self):
        admin = User.objects.create_user(email="admin@example.com", password="pw")
        admin.role, admin.is_active, admin.is_verified = "admin", True, True
        admin.save()
        token = generate_jwt_token(admin)["access"]

        self.gql(REGISTER, {"email": "ann@example.com"})
        emails = [u["email"] for u in self.gql(REQUESTS, token=token)["data"]["getUsersList"]]
        self.assertNotIn("ann@example.com", emails)

        self.gql(VERIFY_OTP, {"email": "ann@example.com", "code": self.code_for("ann@example.com")})
        emails = [u["email"] for u in self.gql(REQUESTS, token=token)["data"]["getUsersList"]]
        self.assertIn("ann@example.com", emails)

    def test_only_admins_can_list_users(self):
        biz = User.objects.create_user(email="biz@example.com", password="pw")
        biz.role, biz.is_active, biz.is_verified = "business", True, True
        biz.save()
        result = self.gql(REQUESTS, token=generate_jwt_token(biz)["access"])
        self.assertIn("errors", result)
