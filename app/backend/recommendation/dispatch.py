"""PostgreSQL-backed dispatch shared by independent hosts and worker processes."""
import os
from math import isfinite
from datetime import timedelta
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.db.models.functions import Now
from .errors import require
from .models import DispatchControl, Job, uid


GPT_METHOD = 'GPT_SELECTION'
LEGACY_GEMINI_METHOD = 'GEMINI_RECONFIGURE'
GPT_JOB_METHODS = (GPT_METHOD, LEGACY_GEMINI_METHOD)
METHODS = {'rule': 'RULE_BASED', 'gpt': GPT_METHOD}
QUEUES = ('all', *METHODS, 'gemini')
# Preserve the shared permit row across a rolling worker update.
PROVIDER_CONTROL = 'gemini'


def provider_settings_valid():
    return (0 < settings.OPENAI_PLAYLIST_TIMEOUT_MS / 1000 < settings.RECOMMENDATION_JOB_TIMEOUT_SECONDS
            and settings.OPENAI_PLAYLIST_MAX_CANDIDATES > 0 and settings.OPENAI_PLAYLIST_MAX_OUTPUT_TOKENS > 0)


def require_provider_configuration():
    require(settings.OPENAI_PLAYLIST_ENABLED and provider_settings_valid()
            and bool(os.environ.get('OPENAI_API_KEY', '').strip()),
            'RECOMMENDATION_PROVIDER_UNAVAILABLE', 'GPT 선곡 서비스 설정을 확인해 주세요.', 503)


def configuration():
    """No connections or writes; all workers must receive the same shared limits."""
    return {
        'recommendation_queue': settings.RECOMMENDATION_QUEUE in QUEUES,
        'recommendation_lease': settings.RECOMMENDATION_LEASE_SECONDS > 0,
        'recommendation_heartbeat': 0 < settings.RECOMMENDATION_HEARTBEAT_SECONDS < settings.RECOMMENDATION_LEASE_SECONDS / 2,
        'recommendation_job_timeout': settings.RECOMMENDATION_JOB_TIMEOUT_SECONDS > settings.RECOMMENDATION_LEASE_SECONDS,
        'recommendation_recoveries': settings.RECOMMENDATION_MAX_RECOVERIES >= 0,
        'recommendation_idle': isfinite(settings.RECOMMENDATION_WORKER_IDLE_SECONDS) and settings.RECOMMENDATION_WORKER_IDLE_SECONDS > 0,
        'recommendation_rule_capacity': settings.RECOMMENDATION_MAX_QUEUED_RULE > 0,
        'recommendation_gpt_capacity': settings.RECOMMENDATION_MAX_QUEUED_GPT > 0,
        'gpt_concurrency': settings.OPENAI_PLAYLIST_MAX_CONCURRENT > 0,
        'gpt_interval': isfinite(settings.OPENAI_PLAYLIST_MIN_INTERVAL_SECONDS) and settings.OPENAI_PLAYLIST_MIN_INTERVAL_SECONDS > 0,
        'gpt_provider_limits': not settings.OPENAI_PLAYLIST_ENABLED or provider_settings_valid(),
    }


def locked_control(name, nonblocking=False):
    DispatchControl.objects.get_or_create(name=name)
    return DispatchControl.objects.select_for_update(
        skip_locked=nonblocking and connection.features.has_select_for_update_skip_locked,
    ).annotate(dispatch_now=Now()).filter(pk=name).first()


def admit(method):
    """Called inside the API transaction, after ownership/idempotency checks."""
    require(connection.in_atomic_block, 'POLICY_NOT_READY', '작업 저장 연결을 확인해 주세요.', 503)
    queue = 'gpt' if method in GPT_JOB_METHODS else 'rule'
    methods = GPT_JOB_METHODS if queue == 'gpt' else (METHODS['rule'],)
    locked_control('admission-' + queue)
    limit = getattr(settings, 'RECOMMENDATION_MAX_QUEUED_' + queue.upper())
    require(Job.objects.filter(method__in=methods, status='QUEUED').count() < limit,
            'RECOMMENDATION_QUEUE_FULL', '추천 요청이 많아요. 잠시 후 다시 시도해 주세요.', 503,
            queue=queue, retry_after_seconds=max(1, int(settings.RECOMMENDATION_WORKER_IDLE_SECONDS)))


def active_job(job):
    return Job.objects.filter(pk=job.pk, status='RUNNING', lease_token=job.lease_token,
                              lease_generation=job.lease_generation, lease_expires_at__gt=Now())


def fail_expired(job, now):
    if job.provider_attempted:
        code, message = 'PROVIDER_OUTCOME_UNKNOWN', '중단된 외부 호출 결과를 확인해야 해요.'
    elif job.recovery_count >= settings.RECOMMENDATION_MAX_RECOVERIES:
        code, message = 'WORKER_RETRY_EXHAUSTED', '작업 복구 한도를 초과했어요.'
    else:
        return False
    job.status = 'FAILED'
    job.error = {'code': code, 'message': message, 'details': {}}
    job.finished_at = now
    job.save(update_fields=['status', 'error', 'finished_at'])
    return True


def claim(queue='all', worker_id=''):
    require(queue in QUEUES, 'POLICY_NOT_READY', '작업자 큐 설정을 확인해 주세요.', 503)
    if queue == 'gemini':
        queue = 'gpt'
    for lane in METHODS if queue == 'all' else (queue,):
        with transaction.atomic():
            provider_lane = lane == 'gpt'
            methods = GPT_JOB_METHODS if provider_lane else (METHODS[lane],)
            gate = locked_control(PROVIDER_CONTROL, nonblocking=True) if provider_lane else None
            if provider_lane and gate is None:
                continue
            for _ in range(32):
                job = Job.objects.select_for_update(
                    skip_locked=connection.features.has_select_for_update_skip_locked,
                ).filter(method__in=methods).filter(
                    Q(status='QUEUED') | Q(status='RUNNING', lease_expires_at__lte=Now()),
                ).annotate(dispatch_now=Now()).order_by('created_at', 'pk').first()
                if job is None:
                    break
                now = job.dispatch_now
                expired = job.status == 'RUNNING'
                if expired and fail_expired(job, now):
                    continue
                if provider_lane:
                    if gate.next_provider_at and gate.next_provider_at > now:
                        break
                    occupied = Job.objects.filter(method__in=methods).exclude(pk=job.pk).filter(
                        Q(status='RUNNING', lease_expires_at__gt=Now()) | Q(provider_lease_until__gt=Now()),
                    ).count()
                    if occupied >= settings.OPENAI_PLAYLIST_MAX_CONCURRENT:
                        break
                    job.method = GPT_METHOD
                job.status = 'RUNNING'
                job.lease_token = uid()
                job.lease_generation += 1
                job.recovery_count += int(expired)
                job.lease_expires_at = now + timedelta(seconds=settings.RECOMMENDATION_LEASE_SECONDS)
                job.worker_id = worker_id[:160]
                job.claimed_at = now
                job.finished_at = None
                job.save()
                return job
    return None


def begin_provider(job):
    """Reserve a call before sending; return delay without holding any DB lock."""
    require_provider_configuration()
    with transaction.atomic():
        gate = locked_control(PROVIDER_CONTROL)
        current = active_job(job).select_for_update().annotate(dispatch_now=Now()).first()
        require(current is not None, 'LEASE_LOST', '작업 실행 권한이 만료됐어요.', 409)
        require(not current.provider_attempted, 'PROVIDER_OUTCOME_UNKNOWN', '중단된 외부 호출 결과를 확인해야 해요.', 503)
        now = current.dispatch_now
        occupied = Job.objects.exclude(pk=job.pk).filter(
            method__in=GPT_JOB_METHODS, provider_lease_until__gt=Now(),
        )
        if occupied.count() >= settings.OPENAI_PLAYLIST_MAX_CONCURRENT:
            return max(0.05, (occupied.order_by('provider_lease_until').first().provider_lease_until - now).total_seconds())
        if gate.next_provider_at and gate.next_provider_at > now:
            return (gate.next_provider_at - now).total_seconds()
        # An interrupted/ambiguous HTTP call retains its permit until timeout + lease grace.
        current.provider_attempted = True
        current.provider_started_at = now
        current.provider_lease_until = now + timedelta(
            seconds=settings.OPENAI_PLAYLIST_TIMEOUT_MS / 1000 + settings.RECOMMENDATION_LEASE_SECONDS)
        current.save(update_fields=['provider_attempted', 'provider_started_at', 'provider_lease_until'])
        gate.next_provider_at = now + timedelta(seconds=settings.OPENAI_PLAYLIST_MIN_INTERVAL_SECONDS)
        gate.save(update_fields=['next_provider_at'])
        return 0


def end_provider(job):
    # A confirmed response frees the permit. Ambiguous failure/crash keeps its deadline.
    Job.objects.filter(pk=job.pk, lease_token=job.lease_token,
                       lease_generation=job.lease_generation).update(provider_lease_until=None)


@transaction.atomic
def try_begin_hourly_provider(job, timeout_seconds):
    """Do not wait behind another job if the optional ordering pool is full."""
    locked_control('hourly-gpt')
    current = active_job(job).select_for_update().annotate(dispatch_now=Now()).first()
    require(current is not None, 'LEASE_LOST', '작업 실행 권한이 만료됐어요.', 409)
    occupied = Job.objects.filter(method='RULE_BASED', provider_lease_until__gt=Now()).count()
    if current.provider_attempted or occupied >= settings.HOURLY_GPT_MAX_CONCURRENT:
        return False
    current.provider_attempted = True
    current.provider_started_at = current.dispatch_now
    current.provider_lease_until = current.dispatch_now + timedelta(
        seconds=timeout_seconds + settings.RECOMMENDATION_LEASE_SECONDS,
    )
    current.save(update_fields=['provider_attempted', 'provider_started_at', 'provider_lease_until'])
    return True
