import logging
from strawberry.extensions import Extension
from graphql_jwt.utils import get_payload
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model
from strawberry.django.context import StrawberryDjangoContext

from strawberry.django.views import GraphQLView
from api.models import User

logger = logging.getLogger(__name__)
User = get_user_model()


class DebugJWTMiddleware(Extension):
    """
    Middleware to handle JWT token parsing and authentication in Strawberry GraphQL.
    Also logs debugging information for development purposes.
    """

    def on_request_start(self):
        request = self.execution_context.context["request"]
        auth_header = request.headers.get("Authorization", "")

        logger.debug(f"Authorization Header: {auth_header}")

        if auth_header.startswith("Bearer "):  # Handle Bearer prefix
            token = auth_header.split("Bearer ")[1]
            try:
                # Decode the JWT token
                payload = get_payload(token)
                logger.debug(f"Decoded Token Payload: {payload}")

                # Fetch the user based on the payload
                # Assuming your token contains `user_id`
                user_id = payload.get("user_id")
                if user_id:
                    try:
                        user = User.objects.get(id=user_id)
                        request.user = user  # Set the authenticated user
                        logger.debug(f"Authenticated user: {user}")
                    except User.DoesNotExist:
                        logger.error(f"No user found with user_id: {user_id}")
                        request.user = AnonymousUser()
                else:
                    logger.error("No user_id found in token payload")
                    request.user = AnonymousUser()

            except Exception as e:
                logger.error(f"Error decoding token: {e}")
                request.user = AnonymousUser()
        else:
            logger.debug("Authorization header missing or invalid")
            request.user = AnonymousUser()

        # Ensure `request.user` exists for unauthenticated requests
        if not hasattr(request, "user"):
            request.user = AnonymousUser()


class CustomGraphQLView(GraphQLView):
    def get_context(self, request):
        return {"request": request}
