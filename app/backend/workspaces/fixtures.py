"""Synthetic provider only: no HTTP client, API key, real registration number or file upload."""
FIXTURES = {
    'demo-active-001': {'business': 'synthetic-business-one', 'location': 'synthetic-location-one', 'match': 'MATCHED', 'status': 'ACTIVE'},
    'demo-branch-002': {'business': 'synthetic-business-one', 'location': 'synthetic-location-two', 'match': 'MATCHED', 'status': 'ACTIVE'},
    'demo-mismatch': {'business': 'synthetic-business-mismatch', 'location': 'synthetic-location-mismatch', 'match': 'NOT_MATCHED', 'status': 'UNKNOWN'},
    'demo-unavailable': {'business': 'synthetic-business-unavailable', 'location': 'synthetic-location-unavailable', 'match': 'UNAVAILABLE', 'status': 'UNKNOWN'},
    'demo-closed': {'business': 'synthetic-business-closed', 'location': 'synthetic-location-closed', 'match': 'MATCHED', 'status': 'CLOSED'},
}
