import json
from unittest import mock

from django.test import Client, TestCase, override_settings

from api.models import BusinessProfile, Listings, User
from api.utils import API_SECRET_KEY, generate_jwt_token


def make_user(email, role, **extra):
    user = User.objects.create_user(email=email, password="password")
    user.role = role
    user.is_active = True
    user.is_admin_approved = True
    for key, value in extra.items():
        setattr(user, key, value)
    user.save()
    return user


def make_business(email, name):
    user = make_user(email, "business")
    BusinessProfile.objects.create(
        user=user, business_name=name, address="1 Main St", city="Toronto",
        state="Ontario", zip_code="M5V1A1", latitude=43.65, longitude=-79.38,
        phone="+14165550100",
    )
    return user


# Same settings alignment as the other API tests: the middleware and the resolvers
# read the API secret from different places under the test settings.
@override_settings(ALLOWED_HOSTS=["*"], API_SECRET_KEY=API_SECRET_KEY)
class AccessControlTests(TestCase):
    """Admin tools are admin-only, and a business can only act on its own items."""

    def setUp(self):
        self.admin = make_user("admin@example.com", "admin")
        self.client_user = make_user("client@example.com", "client")
        self.business = make_business("owner@example.com", "Owner Cafe")
        self.other_business = make_business("other@example.com", "Other Cafe")
        self.listing = Listings.objects.create(
            user=self.business, listing_name="Lunch special", listing_status="active",
        )

    def gql(self, user, query, variables=None):
        token = generate_jwt_token(user)["access"]
        return Client().post(
            "/graphql",
            data=json.dumps({"query": query, "variables": variables or {}}),
            content_type="application/json",
            HTTP_X_API_SECRET=API_SECRET_KEY,
            HTTP_AUTHORIZATION=f"Bearer {token}",
        ).json()

    def assertDenied(self, result):
        self.assertIn("errors", result, result)
        self.assertIn("Access denied", json.dumps(result["errors"]), result)

    # --- admin-only tools ---------------------------------------------------

    def test_only_an_admin_can_list_every_user(self):
        query = "{ listAllUsers { id email } }"
        self.assertDenied(self.gql(self.client_user, query))
        self.assertDenied(self.gql(self.business, query))
        result = self.gql(self.admin, query)
        self.assertNotIn("errors", result, result)
        self.assertEqual(len(result["data"]["listAllUsers"]), User.objects.count())

    def test_only_an_admin_can_suspend_a_business(self):
        mutation = """mutation($input: CreateSuspensionInput!) {
            suspendBusiness(input: $input) { id } }"""
        variables = {"input": {"businessId": self.other_business.id, "reason": "test"}}
        self.assertDenied(self.gql(self.business, mutation, variables))
        self.assertDenied(self.gql(self.client_user, mutation, variables))
        self.assertFalse(BusinessProfile.objects.get(user=self.other_business).is_suspended)

    def test_only_an_admin_can_reactivate_a_business(self):
        mutation = 'mutation { revertSuspendedBusiness(userId: "%s") { success } }' % self.other_business.id
        self.assertDenied(self.gql(self.business, mutation))

    def test_only_an_admin_can_read_suspension_history(self):
        query = "{ getSuspensionHistory(userId: %d) { id } }" % self.other_business.id
        self.assertDenied(self.gql(self.business, query))
        result = self.gql(self.admin, query)
        self.assertNotIn("errors", result, result)

    def test_only_an_admin_can_trigger_the_status_job(self):
        self.assertDenied(self.gql(self.client_user, "mutation { triggerChannelStatusUpdate }"))

    # --- a business can only act on its own items ----------------------------

    def end_listing(self, user):
        return self.gql(user, 'mutation { endListing(listingId: "%s") { id } }' % self.listing.id)

    def test_a_business_cannot_end_another_business_listing(self):
        result = self.end_listing(self.other_business)
        self.assertIn("errors", result, result)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.listing_status, "active")

    def test_the_owner_can_end_their_listing(self):
        result = self.end_listing(self.business)
        self.assertNotIn("errors", result, result)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.listing_status, "phaseout")

    def test_an_admin_can_end_any_listing(self):
        result = self.end_listing(self.admin)
        self.assertNotIn("errors", result, result)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.listing_status, "phaseout")

    # --- support email ------------------------------------------------------

    @mock.patch("api.mutation.SESEmailSender")
    def test_support_email_goes_to_the_callers_own_address(self, sender_cls):
        mutation = """mutation($input: SupportEmailInput!) {
            sendSupportEmailToAdminAndBusiness(input: $input) { success } }"""
        variables = {"input": {
            "subject": "Help", "fullName": "Owner", "designation": "Manager",
            "message": "Hello", "businessEmail": "victim@example.com",
            "businessName": "Someone Else", "businessUuid": "not-mine",
        }}
        result = self.gql(self.business, mutation, variables)
        self.assertNotIn("errors", result, result)

        recipients = [c.kwargs.get("recipient") for c in sender_cls.return_value.send_email.call_args_list]
        self.assertIn("owner@example.com", recipients)
        self.assertNotIn("victim@example.com", recipients)
        sent = json.dumps([str(c) for c in sender_cls.return_value.send_email.call_args_list])
        self.assertNotIn("Someone Else", sent)
        self.assertIn("Owner Cafe", sent)
