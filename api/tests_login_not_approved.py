import json

from django.test import Client, TestCase, override_settings

from api.models import User
from api.utils import API_SECRET_KEY

LOGIN = """
mutation($input: LoginInput!) {
  login(input: $input) { id email role isAdminApproved isVerified }
}
"""
PASSWORD = "Str0ng-Passw0rd!x"


# The middleware checks settings.API_SECRET_KEY and the resolvers check the value
# api.utils imported at startup; under the test settings those differ, so align them.
@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class LoginApprovalStateTests(TestCase):
    """is_admin_approved can be True, False or None (an admin "instigated" the account).
    The login response needs a plain true/false, so None must not break login."""

    def login(self, approved):
        user = User.objects.create_user(email=f"login-{str(approved).lower()}@example.com", password=PASSWORD)
        user.role = "business"
        user.is_active = True
        user.is_verified = True
        user.is_admin_approved = approved
        user.save()
        return Client().post(
            "/graphql",
            data=json.dumps({"query": LOGIN, "variables": {"input": {"email": user.email, "password": PASSWORD}}}),
            content_type="application/json", HTTP_X_API_SECRET=API_SECRET_KEY,
        ).json()

    def test_instigated_account_can_still_log_in_as_not_approved(self):
        result = self.login(None)
        self.assertNotIn("errors", result, result)
        self.assertIs(result["data"]["login"]["isAdminApproved"], False)

    def test_waiting_and_approved_accounts_are_unchanged(self):
        self.assertIs(self.login(False)["data"]["login"]["isAdminApproved"], False)
        self.assertIs(self.login(True)["data"]["login"]["isAdminApproved"], True)
