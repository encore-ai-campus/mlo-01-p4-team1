"""Synthetic provider only: never treated as proof of real business ownership."""
import re
FIXTURES = {
    'demo-linked-venue-000001': {
        'business': 'synthetic-business-venue-000001',
        'location': 'catalog:VN_SL_000001',
        'venue_id': 'VN_SL_000001',
        'match': 'MATCHED',
        'status': 'ACTIVE',
    },
    'demo-active-001': {'business': 'synthetic-business-one', 'location': 'synthetic-location-one', 'match': 'MATCHED', 'status': 'ACTIVE'},
    'demo-branch-002': {'business': 'synthetic-business-one', 'location': 'synthetic-location-two', 'match': 'MATCHED', 'status': 'ACTIVE'},
    'demo-mismatch': {'business': 'synthetic-business-mismatch', 'location': 'synthetic-location-mismatch', 'match': 'NOT_MATCHED', 'status': 'UNKNOWN'},
    'demo-unavailable': {'business': 'synthetic-business-unavailable', 'location': 'synthetic-location-unavailable', 'match': 'UNAVAILABLE', 'status': 'UNKNOWN'},
    'demo-closed': {'business': 'synthetic-business-closed', 'location': 'synthetic-location-closed', 'match': 'MATCHED', 'status': 'CLOSED'},
}


def resolve_fixture(identifier):
    """Map a catalog ID to a stable synthetic scenario, without asserting ownership."""
    fixture = FIXTURES.get(identifier)
    if fixture is not None:
        return fixture
    if not identifier.startswith('catalog:'):
        return None
    venue_id = identifier[len('catalog:'):]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,56}', venue_id):
        return None
    # The legacy demonstration fixture and its catalog ID must share a single
    # business identity, so their claims cannot create duplicate workspaces.
    for existing in FIXTURES.values():
        if existing.get('venue_id') == venue_id:
            return existing
    return {
        'business': f'synthetic-catalog-business:{venue_id}',
        'location': f'catalog:{venue_id}',
        'venue_id': venue_id,
        'match': 'MATCHED',
        'status': 'ACTIVE',
    }
