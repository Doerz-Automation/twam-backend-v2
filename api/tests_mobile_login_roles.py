import json
from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from api.models import OTP, User
from api.utils import API_SECRET_KEY

LOGIN = "mutation($input: LoginInput!) { login(input: $input) { id role } }"
VERIFY = "mutation($email: String!, $otp: String!) { verifyOtp(email: $email, otpCode: $otp) { id role } }"
PASSWORD = "Str0ng-Passw0rd!x"
BUSINESS_MESSAGE = "This is a business account. Please sign in to the TWAM Business Portal on the web."


# The middleware checks settings.API_SECRET_KEY and the resolvers check the value
# api.utils imported at startup; under the test settings those differ, so align them.
@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class MobileLoginRoleTests(TestCase):
    """The mobile app (X-Client-App: mobile) is for consumer accounts only."""

    def make_user(self, role, verified=True):
        user = User.objects.create_user(email=f"{role}-mobile@example.com", password=PASSWORD)
        user.role = role
        user.is_active = True
        user.is_verified = verified
        user.is_admin_approved = True
        user.save()
        return user

    def call(self, query, variables, mobile=True):
        headers = {"HTTP_X_API_SECRET": API_SECRET_KEY}
        if mobile:
            headers["HTTP_X_CLIENT_APP"] = "mobile"
        return Client().post("/graphql", data=json.dumps({"query": query, "variables": variables}),
                             content_type="application/json", **headers).json()

    def login(self, user, password=PASSWORD, mobile=True):
        return self.call(LOGIN, {"input": {"email": user.email, "password": password}}, mobile)

    def test_consumer_can_sign_in_on_mobile(self):
        result = self.login(self.make_user("user"))
        self.assertEqual(result["data"]["login"]["role"], "user", result)

    def test_business_cannot_sign_in_on_mobile(self):
        result = self.login(self.make_user("business"))
        self.assertIn(BUSINESS_MESSAGE, json.dumps(result), result)
        self.assertIsNone(result["data"])

    def test_admin_cannot_sign_in_on_mobile(self):
        result = self.login(self.make_user("admin"))
        self.assertIn("cannot sign in to the mobile app", json.dumps(result), result)

    def test_business_can_still_sign_in_without_the_mobile_header(self):
        result = self.login(self.make_user("business"), mobile=False)
        self.assertEqual(result["data"]["login"]["role"], "business", result)

    def test_wrong_password_does_not_reveal_the_account_type(self):
        result = self.login(self.make_user("business"), password="wrong-password")
        self.assertIn("Invalid email or password", json.dumps(result), result)
        self.assertNotIn("business account", json.dumps(result))

    def test_business_cannot_get_tokens_through_otp_on_mobile(self):
        user = self.make_user("business", verified=False)
        OTP.objects.create(user=user, otp_code="123456", expires_at=timezone.now() + timedelta(minutes=10))
        result = self.call(VERIFY, {"email": user.email, "otp": "123456"})
        self.assertIn(BUSINESS_MESSAGE, json.dumps(result), result)
        user.refresh_from_db()
        self.assertFalse(user.is_verified)  # nothing was changed by the refused request

    def test_business_otp_signup_still_works_from_the_web(self):
        user = self.make_user("business", verified=False)
        OTP.objects.create(user=user, otp_code="123456", expires_at=timezone.now() + timedelta(minutes=10))
        result = self.call(VERIFY, {"email": user.email, "otp": "123456"}, mobile=False)
        self.assertEqual(result["data"]["verifyOtp"]["role"], "business", result)
