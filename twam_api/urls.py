from django.contrib import admin
from django.urls import path
from api.middlewares import CustomGraphQLView
from strawberry.django.views import GraphQLView
from api.schema import schema
from django.views.decorators.csrf import csrf_exempt
from . import views
from .views import test_view
from api.webhooks import stripe_webhook

urlpatterns = [
    path("admin/", admin.site.urls),
    path("graphql/", csrf_exempt(GraphQLView.as_view(schema=schema,
         multipart_uploads_enabled=True))),
    path("graphql", csrf_exempt(GraphQLView.as_view(
        schema=schema,  multipart_uploads_enabled=True))),
    path("graphql/apollo/", csrf_exempt(GraphQLView.as_view(schema=schema,
         multipart_uploads_enabled=True, graphql_ide="apollo-sandbox"))),

    path('', views.health_check),
    path("test/", test_view),
    path("stripe/webhook/", stripe_webhook),
]
