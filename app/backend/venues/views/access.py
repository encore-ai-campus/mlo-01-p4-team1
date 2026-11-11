from functools import wraps

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from backend.recommendation.errors import DomainError, require
from backend.workspaces import authorization
from backend.workspaces.models import ServiceMembership


def standalone_demo():
    return settings.MEMBER_LOCAL_PREVIEW and not settings.PLATFORM_ENABLED


def demo_only(view):
    @never_cache
    @wraps(view)
    def checked(request, *args, **kwargs):
        if not standalone_demo():
            return JsonResponse({"success": False, "code": "NOT_FOUND"}, status=404)
        return view(request, *args, **kwargs)

    return checked


def consumer_mcp(view):
    """Allow the isolated preview and authenticated consumer platform preview."""
    @never_cache
    @wraps(view)
    def checked(request, *args, **kwargs):
        if standalone_demo():
            return view(request, *args, **kwargs)
        if not settings.MEMBER_LOCAL_PREVIEW or not settings.PLATFORM_ENABLED:
            return JsonResponse({"success": False, "code": "NOT_FOUND"}, status=404)
        try:
            authorization.enabled()
            user = authorization.account(request)
            require(ServiceMembership.objects.filter(user=user, kind="CONSUMER", status="ACTIVE").exists(),
                    "CONSUMER_MEMBERSHIP_REQUIRED", "사용자 이용 유형을 먼저 추가해 주세요.", 403)
        except DomainError as error:
            return JsonResponse({"success": False, "code": error.code, "message": error.message}, status=error.status)
        return view(request, *args, **kwargs)
    return checked


def consumer_get(view):
    @never_cache
    @require_GET
    @wraps(view)
    def checked(request, *args, **kwargs):
        if standalone_demo():
            return view(request, *args, consumer=None, **kwargs)
        try:
            authorization.enabled()
            user = authorization.account(request)
            require(
                ServiceMembership.objects.filter(user=user, kind="CONSUMER", status="ACTIVE").exists(),
                "CONSUMER_MEMBERSHIP_REQUIRED",
                "사용자 이용 유형을 먼저 추가해 주세요.",
                403,
            )
        except DomainError as error:
            return JsonResponse(
                {"success": False, "code": error.code, "message": error.message},
                status=error.status,
            )
        return view(request, *args, consumer=user, **kwargs)

    return checked
