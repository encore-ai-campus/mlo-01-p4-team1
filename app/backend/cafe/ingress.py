"""Recognize the viewer HTTPS scheme at the approved CloudFront ingress."""

from ipaddress import IPv4Address

from django.conf import settings


class TrustedCloudFrontScheme:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.META.pop('HTTP_X_MLO_TRUSTED_SCHEME', None)
        if request.META.get('HTTP_CLOUDFRONT_FORWARDED_PROTO') == 'https':
            try:
                peer = IPv4Address(request.META.get('REMOTE_ADDR', ''))
            except ValueError:
                peer = None
            if peer is not None and any(peer in subnet for subnet in settings.PHASE4_TRUSTED_PROXY_CIDRS):
                request.META['HTTP_X_MLO_TRUSTED_SCHEME'] = 'https'
        return self.get_response(request)
