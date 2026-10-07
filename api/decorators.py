from datetime import datetime, timezone as dt_timezone
from functools import wraps
from typing import Callable
import strawberry
from strawberry.types import Info
from .utils import validate_api_secret

from rest_framework_simplejwt.tokens import AccessToken, TokenError
from rest_framework.exceptions import PermissionDenied

def require_api_secret(func):
    @wraps(func)
    def wrapper(self, info: Info, *args, **kwargs):
        validate_api_secret(info)  # Apply the API secret validation
        return func(self, info, *args, **kwargs)
    
    return wrapper

def require_authentication(func: Callable):
    """Decorator to ensure user is authenticated."""
    @wraps(func)
    def wrapper(self, info: Info, *args, **kwargs):
        user = info.context["request"].user
        print(user)
        if not user.is_authenticated:
            raise Exception("Authentication required")
        return func(self, info, *args, **kwargs)
    
    return wrapper

def require_role(roles: list):
    """Decorator to restrict access based on user roles."""
    def decorator(func: Callable):
        @wraps(func)
        def wrapper(self, info: Info, *args, **kwargs):
            user = info.context["request"].user
            if not user.is_authenticated:
                raise Exception("Authentication required")
            
            if user.role not in roles:
                raise Exception(f"Access denied: You must be one of {roles}")
            
            return func(self, info, *args, **kwargs)
        
        return wrapper
    return decorator


def jwt_required(func):
    @wraps(func)
    def wrapper(self, info, *args, **kwargs):
        # Get the token from the Authorization header
        authorization = info.context.request.headers.get('Authorization')
        if not authorization or not authorization.startswith('Bearer '):
            raise PermissionDenied("Authorization token is missing or invalid.")

        token = authorization.split(' ')[1]  # Extract token

        try:
            # Automatically validates signature and expiration
            access_token = AccessToken(token)

            # You can still optionally check expiration manually if needed:
            # if access_token['exp'] < datetime.now(dt_timezone.utc).timestamp():
            #     raise PermissionDenied("Token has expired.")

            user_id = access_token["id"]
            email = access_token["email"]
            role = access_token["role"]

        except TokenError as e:
            raise PermissionDenied(f"Invalid token: {str(e)}")
        except Exception:
            raise PermissionDenied("Authentication failed.")

        # Attach decoded user info to context for use in resolvers
        info.context.user_id = user_id
        info.context.user_email = email
        info.context.user_role = role

        return func(self, info, *args, **kwargs)

    return wrapper


def require_not_suspended(func):
    """Decorator to ensure business is not suspended."""
    @wraps(func)
    def wrapper(self, info: Info, *args, **kwargs):
        from graphql import GraphQLError
        user = info.context.request.user
        if user.is_authenticated and user.role == "business":
            if hasattr(user, 'business_profile') and user.business_profile.is_suspended:
                raise GraphQLError("Your business account is suspended. You cannot perform this action.")
        return func(self, info, *args, **kwargs)
    return wrapper