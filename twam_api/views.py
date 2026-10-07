from django.http import JsonResponse

def health_check(request):
        return JsonResponse({"status": "ok"}, status=200)

def test_view(request):
    user = getattr(request, 'user', None)
    if user and user.is_authenticated:
        return JsonResponse({"message": f"Hello {user.email}"})
    
    return JsonResponse({"message": "Anonymous User"})