import json

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from api.models import OTP, User
from api.utils import API_SECRET_KEY

LOGIN = """mutation($email: String!, $password: String!) {
  login(input: {email: $email, password: $password}) { id accessToken } }"""
VERIFY_OTP = """mutation($email: String!, $code: String!) {
  verifyOtp(email: $email, otpCode: $code) { id } }"""
REGISTER = """mutation($email: String!) {
  register(input: {email: $email, password: "Password123!", role: "user"}) { id } }"""


@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class RateLimitTests(TestCase):
    """Password and code guessing is cut off; normal use is not affected."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email="person@example.com", password="right-password")
        self.user.role = "client"
        self.user.is_active = True
        self.user.is_verified = True
        self.user.save()

    def gql(self, query, variables, ip="203.0.113.10"):
        return Client().post(
            "/graphql",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
            HTTP_X_API_SECRET=API_SECRET_KEY,
            HTTP_X_FORWARDED_FOR=ip,
        ).json()

    def login(self, password, email="person@example.com", ip="203.0.113.10"):
        return self.gql(LOGIN, {"email": email, "password": password}, ip)

    def assertLimited(self, result):
        self.assertIn("Too many attempts", json.dumps(result.get("errors")), result)

    # --- login ----------------------------------------------------------------

    def test_five_wrong_passwords_lock_the_account_for_a_while(self):
        for _ in range(5):
            result = self.login("wrong")
            self.assertIn("errors", result)
            self.assertNotIn("Too many attempts", json.dumps(result["errors"]))
        # even the right password is refused until the window ends
        self.assertLimited(self.login("right-password"))

    def test_the_lock_is_per_account(self):
        for _ in range(5):
            self.login("wrong")
        other = User.objects.create_user(email="other@example.com", password="pw")
        other.role, other.is_active, other.is_verified = "client", True, True
        other.save()
        result = self.login("pw", email="other@example.com", ip="198.51.100.7")
        self.assertNotIn("errors", result, result)

    def test_a_successful_login_clears_earlier_mistakes(self):
        for _ in range(4):
            self.login("wrong")
        self.assertNotIn("errors", self.login("right-password"))
        for _ in range(4):
            self.login("wrong")
        # 4 + success + 4 never reaches 5 in a row
        self.assertNotIn("errors", self.login("right-password"))

    def test_the_email_is_matched_whatever_its_case(self):
        for _ in range(5):
            self.login("wrong", email="Person@Example.com ")
        self.assertLimited(self.login("right-password"))

    def test_one_ip_cannot_guess_across_many_accounts(self):
        for i in range(30):
            self.login("wrong", email=f"user{i}@example.com")
        self.assertLimited(self.login("right-password"))

    # --- sign-up / reset codes --------------------------------------------------

    def test_guessing_the_otp_is_cut_off(self):
        from datetime import timedelta
        from django.utils import timezone
        OTP.objects.create(user=self.user, otp_code="123456",
                           expires_at=timezone.now() + timedelta(minutes=10))
        for code in ("000001", "000002", "000003", "000004", "000005"):
            result = self.gql(VERIFY_OTP, {"email": "person@example.com", "code": code})
            self.assertIn("errors", result)
        # the right code no longer helps once the limit is hit
        self.assertLimited(self.gql(VERIFY_OTP, {"email": "person@example.com", "code": "123456"}))

    # --- sign-up ------------------------------------------------------------------

    def test_one_ip_can_only_create_ten_accounts_an_hour(self):
        for i in range(10):
            result = self.gql(REGISTER, {"email": f"new{i}@example.com"})
            self.assertNotIn("Too many attempts", json.dumps(result.get("errors")), result)
        self.assertLimited(self.gql(REGISTER, {"email": "new10@example.com"}))
        # a different IP is unaffected
        result = self.gql(REGISTER, {"email": "new11@example.com"}, ip="198.51.100.7")
        self.assertNotIn("Too many attempts", json.dumps(result.get("errors")), result)
