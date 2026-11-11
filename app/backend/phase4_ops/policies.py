from dataclasses import dataclass

from django.conf import settings

from .errors import require


@dataclass(frozen=True)
class SongPolicy:
    version: str
    allow_general_users: bool
    require_approved_venue: bool
    quota_scope: str
    window_days: int | None
    period_limit: int | None
    count_all_submitted: bool

    def public(self):
        return {'ready': True, 'version': self.version, 'policy_version': self.version,
                'per_submission_limit': 20, 'period_limit': self.period_limit,
                'window_days': self.window_days, 'quota_scope': self.quota_scope,
                'allow_general_users': self.allow_general_users,
                'require_approved_venue': self.require_approved_venue}


def song_policy(required=True):
    raw = getattr(settings, 'PHASE4_SONG_POLICY', None)
    valid = type(raw) is dict and set(raw) == set(SongPolicy.__dataclass_fields__)
    if valid:
        valid = (type(raw['version']) is str and 0 < len(raw['version']) <= 128
                 and type(raw['allow_general_users']) is bool
                 and type(raw['require_approved_venue']) is bool
                 and raw['quota_scope'] in {'account', 'venue'}
                 and ((raw['window_days'] is None and raw['period_limit'] is None)
                      or (type(raw['window_days']) is int and 1 <= raw['window_days'] <= 366
                          and type(raw['period_limit']) is int and 1 <= raw['period_limit'] <= 10000))
                 and raw['count_all_submitted'] is True
                 and (raw['quota_scope'] != 'venue' or raw['require_approved_venue']))
    if valid:
        return SongPolicy(**raw)
    if required:
        require(False, 'POLICY_NOT_READY', '곡 신청 자격·기간 한도·차감 정책의 연결이 필요합니다.', 503)
    return None


def policy_status():
    policy = song_policy(required=False)
    return policy.public() if policy else {
        'ready': False, 'version': None, 'policy_version': None, 'per_submission_limit': 20,
        'period_limit': None, 'window_days': None, 'quota_scope': None,
        'allow_general_users': None, 'require_approved_venue': None,
    }
