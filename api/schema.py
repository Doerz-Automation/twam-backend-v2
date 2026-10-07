import strawberry
from strawberry.types import Info
from django.contrib.auth import authenticate, login, logout
from django.utils.crypto import get_random_string
from django.core.mail import send_mail
from django.utils.timezone import now, timedelta
from django.http import HttpRequest
from api.middlewares import DebugJWTMiddleware
from api.mutation import Mutation
from api.query import Query
import logging

logger = logging.getLogger(__name__)

schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[DebugJWTMiddleware],  # Middleware is added as an extension
)


def get_context(request: HttpRequest):
    return {"request": request}
