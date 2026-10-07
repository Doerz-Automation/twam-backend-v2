from django.conf import settings
from django.http import JsonResponse
import json

class ApiSecretMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 1. If the request is NOT /graphql/, allow it without validation
        if not request.path.startswith('/graphql'):
            return self.get_response(request)

        # 2. If request body is invalid JSON, allow it without API_SECRET validation
        try:
            json.loads(request.body.decode("utf-8"))  # Just checking if it's valid JSON
        except json.JSONDecodeError:
            return self.get_response(request)

        # 3. Validate X-API-SECRET for all valid GraphQL requests
        secret_key = request.headers.get("X-API-SECRET")
        if secret_key != settings.API_SECRET_KEY:
            return JsonResponse({"error": "Invalid or missing API secret"}, status=403)

        return self.get_response(request)
