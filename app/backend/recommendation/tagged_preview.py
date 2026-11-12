"""Rules for the full local catalog projected from the tagging JSON."""
from django.conf import settings


TAGGED_PREVIEW_SOURCE_PREFIX = 'local-preview:tagged-json:'
EXTRA_TAGGED_GENRES = {
    'ROCK_PUNK_METAL': '록·펑크·메탈',
    'FOLK_COUNTRY_BLUES': '포크·컨트리·블루스',
}


def full_tagged_preview_version(digest):
    return f'tagged-json-preview-{digest[:16]}-v2'


def is_full_tagged_preview(catalog):
    return bool(catalog and settings.LOCAL_DEMO and not settings.INTEGRATION_ENABLED and catalog.is_test
                and catalog.source_ref == TAGGED_PREVIEW_SOURCE_PREFIX + catalog.source_sha256
                and catalog.pk == full_tagged_preview_version(catalog.source_sha256))
