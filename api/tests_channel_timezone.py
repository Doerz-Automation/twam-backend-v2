from django.test import TestCase
from django.utils import timezone
from datetime import datetime, timedelta, date
from django.contrib.auth import get_user_model
from api.models import AdvertisementChannel, BusinessProfile
from api.scheduler import update_channel_status
import pytz

User = get_user_model()

class ChannelTimezoneTests(TestCase):
    def setUp(self):
        # Create a test user and business
        self.user = User.objects.create(email="test_tz@example.com")
        self.business = BusinessProfile.objects.create(user=self.user, business_name="TZ Business")

    def create_channel(self, name, tz_name, end_date, expiry_at):
        return AdvertisementChannel.objects.create(
            business=self.business,
            channel_name=name,
            status="active",
            timezone=tz_name,
            end_date=end_date,
            expiry_at=expiry_at,
            price_per_day=50
        )

    def test_utc_exact_expiry(self):
        """
        Test Case 1: UTC Exact Expiry.
        Shows how the database now simply compares expiry_at against UTC NOW().
        """
        
        real_now = timezone.now() # This is a UTC aware datetime
        
        # Scenario: A Toronto channel that is supposed to expire in 5 minutes
        toronto_expiry = real_now + timedelta(minutes=5)
        
        # Scenario: a Tokyo channel that was supposed to expire 5 minutes ago
        tokyo_expiry = real_now - timedelta(minutes=5)
        
        channel_toronto = self.create_channel("Toronto Channel", "America/Toronto", toronto_expiry.date(), toronto_expiry)
        channel_tokyo = self.create_channel("Tokyo Channel", "Asia/Tokyo", tokyo_expiry.date(), tokyo_expiry)
        
        # Run the scheduler
        update_channel_status()
        
        # Refresh from DB
        channel_toronto.refresh_from_db()
        channel_tokyo.refresh_from_db()
        
        # Toronto should STILL BE ACTIVE (UTC now is before expiry_at)
        self.assertEqual(channel_toronto.status, "active", "Toronto channel expired prematurely before its exact UTC expiry_at.")
        
        # Tokyo should BE PHASEOUT (UTC now is after expiry_at)
        self.assertEqual(channel_tokyo.status, "phaseout", "Tokyo channel failed to expire after its exact UTC expiry_at.")

