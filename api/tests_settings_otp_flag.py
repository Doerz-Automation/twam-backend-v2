from types import SimpleNamespace

from django.test import TestCase, override_settings
from graphql import GraphQLError

from api.decorators import assert_settings_verified
from api.models import User


class SettingsOtpFlagTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="otp-flag@example.com", password="password")
        self.user.role = "business"
        self.user.save()
        request = SimpleNamespace(user=self.user, headers={}, META={})
        self.info = SimpleNamespace(context=SimpleNamespace(request=request))

    def test_otp_required_when_flag_is_not_set(self):
        with override_settings(DISABLE_SETTINGS_OTP=False):
            with self.assertRaises(GraphQLError):
                assert_settings_verified(self.info)

    def test_otp_skipped_only_when_flag_is_on(self):
        with override_settings(DISABLE_SETTINGS_OTP=True):
            self.assertIsNone(assert_settings_verified(self.info))
