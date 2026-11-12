"""Serve only the immutable Vite build shipped in the web image."""
import mimetypes
from pathlib import Path
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse
from django.views.decorators.http import require_safe


def frontend_ready():
    root = Path(settings.FRONTEND_DIST)
    return (root/'index.html').is_file() and (root/'assets').is_dir()


@require_safe
def index(request):
    if not frontend_ready():
        return HttpResponse('사용자 화면 빌드를 준비하고 있습니다.', status=503, content_type='text/plain; charset=utf-8')
    response = FileResponse((Path(settings.FRONTEND_DIST)/'index.html').open('rb'), content_type='text/html; charset=utf-8')
    response['Cache-Control'] = 'no-store'
    # Keep account/recovery/passkey routes strictly no-referrer, while allowing origin referrer for map tile CDNs on venue routes.
    normalized_path = request.path.rstrip('/') or '/'
    if normalized_path in ('/venues', '/platform/consumer') or normalized_path.startswith('/venues/'):
        response['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    else:
        response['Referrer-Policy'] = 'no-referrer'
    return response


@require_safe
def asset(request, name, prefix=''):
    root = Path(settings.FRONTEND_DIST).resolve()
    target = (root/prefix/name).resolve()
    if (not target.is_relative_to(root) or not target.is_file()
            or target.suffix.lower() not in {'.js','.css','.svg','.png','.jpg','.jpeg','.webp','.ico','.woff','.woff2'}):
        raise Http404()
    response = FileResponse(target.open('rb'), content_type=mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
    response['Cache-Control'] = 'public, max-age=31536000, immutable' if prefix == 'assets' else 'public, max-age=300'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cross-Origin-Resource-Policy'] = 'same-origin'
    return response
