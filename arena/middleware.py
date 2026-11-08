from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.db import DatabaseError
from django.http import HttpResponse
from django.utils import timezone
from .rate_limits import client_address, consume

class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        response['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        if request.path.startswith(('/p/', '/captain/', '/organizer/', '/admin/', '/login/', '/signup/', '/auth/', '/account/', '/team/', '/register/')):
            response['Cache-Control'] = 'no-store, private'
            response['X-Robots-Tag'] = 'noindex, nofollow'
        return response

class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Telegram shares sender IPs across users. Its exact endpoint verifies
        # the configured secret, bounds the body, and serializes/deduplicates.
        if request.method == 'POST' and request.path != '/telegram/webhook/':
            is_auth = request.path in ('/login/', '/signup/', '/admin/login/') or request.path.startswith('/auth/')
            limit, seconds = (20, 900) if is_auth else (120, 60)
            try:
                allowed, retry_after = consume(client_address(request), 'auth' if is_auth else 'post', limit, seconds)
            except DatabaseError:
                response = HttpResponse('Сервис временно недоступен. Повторите попытку позже.', status=503)
                response['Retry-After'] = '5'
                response['Cache-Control'] = 'no-store'
                return response
            if not allowed:
                response = HttpResponse('Слишком много запросов. Подождите и повторите попытку.', status=429)
                response['Retry-After'] = str(retry_after)
                response['Cache-Control'] = 'no-store'
                return response
        return self.get_response(request)

class TournamentTimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .models import Tournament
        name = Tournament.objects.values_list('timezone_name', flat=True).first()
        try:
            timezone.activate(ZoneInfo(name or 'UTC'))
        except (ZoneInfoNotFoundError, ValueError):
            timezone.deactivate()
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
