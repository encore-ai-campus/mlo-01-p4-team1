import re

from django.db import transaction

from .errors import require
from .models import AccountLock, ActionReceipt, History
from .validation import fingerprint


def lock(key):
    AccountLock.objects.get_or_create(key=key)
    return AccountLock.objects.select_for_update().get(key=key)


def record(entity_type, entity_id, actor_id, event_type, revision, **data):
    History.objects.create(entity_type=entity_type, entity_id=str(entity_id), actor_id=actor_id,
                           event_type=event_type, revision=revision, data=data)


def mutate(actor, scope, key, body, operation):
    require(type(key) is str and re.fullmatch(r'[A-Za-z0-9._:-]{8,200}', key) is not None,
            'IDEMPOTENCY_KEY_REQUIRED', '요청 식별 키가 필요합니다.')
    digest = fingerprint(body)
    with transaction.atomic():
        lock('account:' + actor.account_id)
        cached = ActionReceipt.objects.filter(account_id=actor.account_id, scope=scope, key=key).first()
        if cached:
            require(cached.fingerprint == digest, 'IDEMPOTENCY_CONFLICT', '같은 키의 요청 내용이 다릅니다.', 409)
            return cached.response, cached.status
        response, status = operation()
        ActionReceipt.objects.create(account_id=actor.account_id, scope=scope, key=key,
                                     fingerprint=digest, response=response, status=status)
        return response, status
