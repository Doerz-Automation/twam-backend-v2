import pytest
from unittest.mock import patch, MagicMock
from django.conf import settings
from django.utils import timezone
from api.models import User, BusinessProfile, SupportTicket
from api.mutation import Mutation
from strawberry.types import Info

@pytest.fixture
def business_user(db):
    user = User.objects.create_user(email="business@example.com", password="password")
    user.role = "business"
    user.save()
    profile = BusinessProfile.objects.create(
        user=user,
        business_name="Test Business",
    )
    return user

@pytest.mark.django_db
class TestSupportEmail:
    @patch("api.mutation.SESEmailSender")
    @patch("api.decorators.AccessToken")
    def test_send_support_email_includes_business_info(self, mock_access_token, mock_ses_class, business_user):
        mock_ses_instance = mock_ses_class.return_value
        
        # Mock AccessToken to return dummy data
        mock_token_instance = MagicMock()
        mock_token_instance.__getitem__.side_effect = lambda key: {
            "id": business_user.id,
            "email": business_user.email,
            "role": business_user.role
        }.get(key)
        mock_access_token.return_value = mock_token_instance

        mutation = Mutation()
        
        # Mocking info.context to work as both dict and object
        mock_info = MagicMock(spec=Info)
        mock_request = MagicMock()
        mock_request.user = business_user
        mock_request.headers = {
            'X-API-SECRET': settings.API_SECRET_KEY,
            'Authorization': 'Bearer dummy_token'
        }
        
        # This part is tricky because decorators access context in different ways
        mock_context = MagicMock()
        mock_context.request = mock_request
        mock_context.__getitem__.side_effect = lambda key: {"request": mock_request}.get(key)
        
        mock_info.context = mock_context
        
        input_data = MagicMock()
        input_data.subject = "Technical Issues"
        input_data.full_name = "John Doe"
        input_data.designation = "Manager"
        input_data.message = "I need help with map integration."
        input_data.images = []
        input_data.business_name = "Explicit Business"
        input_data.business_email = "explicit@example.com"
        input_data.business_uuid = "12345678-1234-5678-1234-567812345678"

        response = mutation.send_support_email_to_admin_and_business(mock_info, input_data)
        
        assert response.success is True
        
        # Verify SES send_email was called
        assert mock_ses_instance.send_email.called
        
        # Check call arguments for admin email
        admin_call_args = mock_ses_instance.send_email.call_args_list[0]
        _, kwargs = admin_call_args
        
        body_text = kwargs["body_text"]
        body_html = kwargs["body_html"]
        
        # Verify business info is in the email bodies
        assert "Name: Explicit Business" in body_text
        assert "Email: explicit@example.com" in body_text
        assert "UUID: 12345678-1234-5678-1234-567812345678" in body_text
        
        assert "<b>Name:</b> Explicit Business" in body_html
        assert "<b>Email:</b> explicit@example.com" in body_html
        assert "<b>UUID:</b> 12345678-1234-5678-1234-567812345678" in body_html
        
        # Verify SupportTicket was created
        ticket = SupportTicket.objects.filter(full_name="John Doe").first()
        assert ticket is not None
        assert ticket.business == business_user.business_profile
        assert ticket.subject == "Technical Issues"
        assert ticket.business_name == "Explicit Business"
        assert ticket.business_email == "explicit@example.com"
        assert str(ticket.business_uuid) == "12345678-1234-5678-1234-567812345678"

    @patch("api.mutation.SESEmailSender")
    @patch("api.decorators.AccessToken")
    def test_send_support_email_fallback_to_profile(self, mock_access_token, mock_ses_class, business_user):
        mock_ses_instance = mock_ses_class.return_value
        
        # Mock AccessToken
        mock_token_instance = MagicMock()
        mock_token_instance.__getitem__.side_effect = lambda key: {
            "id": business_user.id,
            "email": business_user.email,
            "role": business_user.role
        }.get(key)
        mock_access_token.return_value = mock_token_instance

        mutation = Mutation()
        mock_info = MagicMock(spec=Info)
        mock_request = MagicMock()
        mock_request.user = business_user
        mock_request.headers = {
            'X-API-SECRET': settings.API_SECRET_KEY,
            'Authorization': 'Bearer dummy_token'
        }
        mock_context = MagicMock()
        mock_context.request = mock_request
        mock_context.__getitem__.side_effect = lambda key: {"request": mock_request}.get(key)
        mock_info.context = mock_context
        
        input_data = MagicMock()
        input_data.subject = "Fallback Test"
        input_data.full_name = "Jane Doe"
        input_data.designation = "Lead"
        input_data.message = "Testing fallback logic."
        input_data.images = []
        # Missing business fields in input
        input_data.business_name = None
        input_data.business_email = None
        input_data.business_uuid = None

        response = mutation.send_support_email_to_admin_and_business(mock_info, input_data)
        assert response.success is True
        
        ticket = SupportTicket.objects.filter(full_name="Jane Doe").first()
        assert ticket.business_name == "Test Business"
        assert ticket.business_email == "business@example.com"
        assert ticket.business_uuid == business_user.business_profile.business_uuid
