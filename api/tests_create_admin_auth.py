import json

from django.test import Client, TestCase, override_settings

from api.models import User
from api.utils import API_SECRET_KEY, generate_jwt_token

CREATE_ADMIN = """
mutation($email: String!, $password: String!) {
  createAdmin(firstName: "New", lastName: "Admin", email: $email, password: $password) { id email role }
}
"""
STRONG = "Str0ng-Passw0rd!x"


# The middleware checks settings.API_SECRET_KEY and the resolvers check the value
# api.utils imported at startup; under the test settings those differ, so align them.
@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class CreateAdminAuthTests(TestCase):
    def setUp(self):
        self.admin = self.make_user("admin-boss@example.com", "admin")
        self.business = self.make_user("biz@example.com", "business")

    def make_user(self, email, role):
        user = User.objects.create_user(email=email, password="password")
        user.role = role
        user.is_active = True
        user.save()
        return user

    def call(self, email="new-admin@example.com", password=STRONG, as_user=None):
        headers = {"HTTP_X_API_SECRET": API_SECRET_KEY}
        if as_user:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {generate_jwt_token(as_user)['access']}"
        return Client().post(
            "/graphql", data=json.dumps({"query": CREATE_ADMIN, "variables": {"email": email, "password": password}}),
            content_type="application/json", **headers,
        ).json()

    def admins(self, email="new-admin@example.com"):
        return User.objects.filter(email=email, role="admin")

    def test_api_secret_alone_cannot_create_an_admin(self):
        result = self.call()
        self.assertIn("Authentication required", json.dumps(result), result)
        self.assertFalse(self.admins().exists())

    def test_a_business_cannot_create_an_admin(self):
        result = self.call(as_user=self.business)
        self.assertIn("Access denied", json.dumps(result), result)
        self.assertFalse(self.admins().exists())

    def test_an_admin_can_create_an_admin(self):
        result = self.call(as_user=self.admin)
        self.assertEqual(result["data"]["createAdmin"]["role"], "admin", result)
        self.assertTrue(self.admins().exists())

    def test_email_is_normalised_and_duplicates_are_refused(self):
        self.call(email="  Mixed.Case@Example.com ", as_user=self.admin)
        self.assertTrue(self.admins("mixed.case@example.com").exists())
        again = self.call(email="mixed.case@example.com", as_user=self.admin)
        self.assertIn("already exists", json.dumps(again), again)

    def test_weak_password_is_refused(self):
        result = self.call(password="123", as_user=self.admin)
        self.assertIn("errors", result, result)
        self.assertFalse(self.admins().exists())
