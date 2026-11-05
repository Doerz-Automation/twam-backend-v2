import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from api.models import BusinessCategory, User
from api.utils import API_SECRET_KEY, generate_jwt_token

UPDATE = "mutation($input: UpdateCategoryInput!) { updateBusinessCategory(input: $input) { id name } }"
DELETE = "mutation($id: ID!) { deleteBusinessCategory(id: $id) }"
TRIGGER = "mutation { triggerChannelStatusUpdate }"


# The middleware checks settings.API_SECRET_KEY and the resolvers check the value
# api.utils imported at startup; under the test settings those differ, so align them.
@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class CategoryMutationAuthTests(TestCase):
    """update/delete category and the status-update trigger need a login, not just the API secret."""

    def setUp(self):
        self.category = BusinessCategory.objects.create(name="Test category")
        self.admin = User.objects.create_user(email="admin-cat@example.com", password="password")
        self.admin.role = "admin"
        self.admin.is_active = True
        self.admin.save()

    def call(self, query, variables=None, token=True):
        headers = {"HTTP_X_API_SECRET": API_SECRET_KEY}
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {generate_jwt_token(self.admin)['access']}"
        return Client().post(
            "/graphql", data=json.dumps({"query": query, "variables": variables or {}}),
            content_type="application/json", **headers,
        ).json()

    def test_api_secret_alone_is_not_enough(self):
        with patch("api.scheduler.update_channel_status") as job:
            results = [
                self.call(UPDATE, {"input": {"id": self.category.id, "cat": "Hacked"}}, token=False),
                self.call(DELETE, {"id": str(self.category.id)}, token=False),
                self.call(TRIGGER, token=False),
            ]
        for result in results:
            self.assertIn("errors", result, result)
            self.assertIn("Authentication required", json.dumps(result), result)
        self.assertTrue(BusinessCategory.objects.filter(pk=self.category.pk).exists())
        job.assert_not_called()

    def test_missing_api_secret_is_still_refused(self):
        response = Client().post(
            "/graphql", data=json.dumps({"query": DELETE, "variables": {"id": str(self.category.id)}}),
            content_type="application/json",
        ).json()
        self.assertTrue(response.get("error") or response.get("errors"), response)
        self.assertTrue(BusinessCategory.objects.filter(pk=self.category.pk).exists())

    def test_logged_in_user_can_still_use_them(self):
        with patch("api.scheduler.update_channel_status") as job:
            trigger = self.call(TRIGGER)
        self.assertEqual(trigger["data"]["triggerChannelStatusUpdate"], "Channel status update triggered successfully")
        job.assert_called_once()

        deleted = self.call(DELETE, {"id": str(self.category.id)})
        self.assertTrue(deleted["data"]["deleteBusinessCategory"], deleted)
        self.assertFalse(BusinessCategory.objects.filter(pk=self.category.pk).exists())
