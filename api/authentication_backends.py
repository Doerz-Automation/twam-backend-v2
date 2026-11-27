from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from api.models import User
import logging

from api.email_utils import normalize_email

User = get_user_model()
logger = logging.getLogger(__name__)

class EmailBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        email = normalize_email(username)
        logger.debug(f"Authenticating user with email: {email}")
        if not email:
            return None

        try:
            user = User.objects.get_by_email(email)
        except User.DoesNotExist:
            logger.debug(f"No user found with email: {email}")
            return None

        if user.check_password(password) and self.user_can_authenticate(user):
            logger.debug(f"Authenticated user: {user.email}")
            return user
        logger.debug(f"Authentication failed for email: {email}")
        return None