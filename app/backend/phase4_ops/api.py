import hmac
import ipaddress
import json
from functools import wraps

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError, IntegrityError
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt, csrf_protect

from . import pipeline_services, registration_services, song_services
from . import serialization as out
from .access import managed_venues, principal
from .errors import OperationError, require
from .models import PipelineTask, SongItem, SongSubmission
from .policies import policy_status
from .transactions import mutate
from .validation import shape


def csrf_failure(request, reason=''):
    return JsonResponse({'code': 'CSRF_FAILED', 'message': '요청 인증을 다시 확인해 주세요.', 'details': {}}, status=403)


def endpoint(methods, capability=None, worker=False, session=False):
    def decorate(function):
        @wraps(function)
        def view(request, *args, **kwargs):
            try:
                require(request.method in methods, 'METHOD_NOT_ALLOWED', '지원하지 않는 요청입니다.', 405)
                if worker:
                    worker_auth(request)
                    actor = None
                else:
                    try:
                        actor = principal(request, capability)
                    except OperationError as error:
                        if not (session and error.status == 401):
                            raise
                        actor = None
                result = function(request, actor, *args, **kwargs)
                data, status = result if type(result) is tuple else (result, 200)
                response = JsonResponse(data, status=status)
            except OperationError as error:
                response = JsonResponse({'code': error.code, 'message': error.message, 'details': error.details}, status=error.status)
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError, RequestDataTooBig):
                response = JsonResponse({'code': 'INVALID_INPUT', 'message': '요청 형식과 크기를 확인해 주세요.', 'details': {}}, status=400)
            except IntegrityError:
                response = JsonResponse({'code': 'STORAGE_CONFLICT', 'message': '요청 처리 상태를 확인한 뒤 다시 시도해 주세요.', 'details': {}}, status=409)
            except DatabaseError:
                response = JsonResponse({'code': 'STORAGE_UNAVAILABLE', 'message': '저장소 연결을 확인한 뒤 다시 시도해 주세요.', 'details': {}}, status=503)
            response['Cache-Control'] = 'no-store'
            response['X-Content-Type-Options'] = 'nosniff'
            return response
        return csrf_exempt(view) if worker else csrf_protect(view)
    return decorate


def worker_auth(request):
    expected = getattr(settings, 'PHASE4_WORKER_TOKEN', '')
    require(type(expected) is str and len(expected) >= 24, 'WORKER_NOT_READY', '작업자 인증 연결이 필요합니다.', 503)
    if not request.is_secure():
        allowed = getattr(settings, 'PHASE4_ALLOW_INSECURE_WORKER_LOCAL', False)
        try:
            loopback = ipaddress.ip_address(request.META.get('REMOTE_ADDR', '')).is_loopback
        except ValueError:
            loopback = False
        require(allowed and loopback, 'HTTPS_REQUIRED', '작업자 연결은 HTTPS가 필요합니다.', 403)
    require(hmac.compare_digest(request.headers.get('Authorization', '').encode(), ('Bearer ' + expected).encode()),
            'AUTH_REQUIRED', '작업자 인증이 필요합니다.', 401)


def body(request):
    require(len(request.body) <= 131072, 'PAYLOAD_TOO_LARGE', '요청 크기를 확인해 주세요.', 413)
    require(request.content_type == 'application/json', 'INVALID_CONTENT_TYPE', 'JSON 요청이 필요합니다.', 415)
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError('duplicate JSON member')
            result[key] = value
        return result
    def bad_constant(value):
        raise ValueError('nonfinite JSON value')
    return json.loads(request.body.decode('utf-8'), object_pairs_hook=pairs, parse_constant=bad_constant)


def mutation(request, actor, scope, data, function):
    return mutate(actor, scope, request.headers.get('Idempotency-Key', ''), data, function)


def page(request):
    limit = int(request.GET.get('limit', '20'))
    offset = int(request.GET.get('offset', '0'))
    require(1 <= limit <= 100 and 0 <= offset <= 100000, 'INVALID_INPUT', '조회 범위를 확인해 주세요.')
    return limit, offset


def listing(queryset, serializer, request):
    limit, offset = page(request)
    rows = list(queryset[offset:offset + limit + 1])
    return {'items': [serializer(row) for row in rows[:limit]], 'has_more': len(rows) > limit}


@endpoint({'GET'}, session=True)
def session(request, actor):
    return {'authenticated': actor is not None, 'account_id': actor.account_id if actor else None,
            'capabilities': sorted(actor.capabilities) if actor else [],
            'managed_venues': managed_venues(actor.account_id) if actor else [],
            'song_policy': policy_status(), 'csrf_token': get_token(request)}


@endpoint({'GET'})
def song_quota(request, actor):
    return song_services.quota(actor, request.GET.get('venue_id') or None)


@endpoint({'POST'})
def song_check(request, actor):
    data = body(request)
    return song_services.check(actor, data)


@endpoint({'GET', 'POST'})
def song_requests(request, actor):
    if request.method == 'GET':
        return listing(SongSubmission.objects.filter(account_id=actor.account_id).prefetch_related('items').order_by('-created_at'),
                       out.submission, request)
    data = body(request)
    return mutation(request, actor, 'song-submit', data, lambda: song_services.submit(actor, data))


@endpoint({'GET'})
def song_request_detail(request, actor, pk):
    row = SongSubmission.objects.filter(pk=pk, account_id=actor.account_id).first()
    require(row is not None, 'NOT_FOUND', '신청을 찾을 수 없습니다.', 404)
    return out.submission(row)


@endpoint({'GET'}, 'review_songs')
def admin_songs(request, actor):
    rows = SongItem.objects.select_related('submission').order_by('-submission__created_at', 'position')
    if request.GET.get('status'):
        require(request.GET['status'] in {'PENDING', 'HOLD', 'APPROVED', 'REJECTED', 'LINKED'}, 'INVALID_INPUT', '상태를 확인해 주세요.')
        rows = rows.filter(review_status=request.GET['status'])
    return listing(rows, lambda row: out.song_item(row, operator=True), request)


@endpoint({'POST'}, 'review_songs')
def admin_song_review(request, actor, pk):
    data = body(request)
    return mutation(request, actor, 'song-review:' + str(pk), data, lambda: song_services.review(actor, pk, data))


@endpoint({'GET'}, 'review_songs')
def admin_tasks(request, actor):
    rows = PipelineTask.objects.select_related('item', 'receipt').order_by('-updated_at', 'id')
    result = listing(rows, lambda row: out.task(row, operator=True), request)
    options = pipeline_services.tag_dictionary(required=False)
    if options:
        result['tag_options'] = {kind: sorted(codes) for kind, codes in options.items()}
    return result


@endpoint({'POST'}, 'review_songs')
def admin_task_finalize(request, actor, pk):
    data = body(request)
    return mutation(request, actor, 'task-finalize:' + str(pk), data,
                    lambda: registration_services.finalize(actor, pk, data))


@endpoint({'GET'}, worker=True)
def worker_tasks(request, actor):
    rows = PipelineTask.objects.filter(item__review_status='APPROVED').select_related('item').order_by('updated_at', 'id')
    from django.db.models import Q
    rows = rows.filter(Q(status='READY') | Q(status='RUNNING', lease_expires_at__lte=timezone.now()))
    return listing(rows, out.task, request)


@endpoint({'POST'}, worker=True)
def worker_claim(request, actor, pk):
    shape(body(request), [])
    return pipeline_services.claim(pk)


@endpoint({'POST'}, worker=True)
def worker_receive(request, actor):
    data = body(request)
    require(type(data) is dict, 'INVALID_INPUT', '결과 객체가 필요합니다.')
    require(type(data.get('result_id')) is str and request.headers.get('Idempotency-Key') == data['result_id'],
            'IDEMPOTENCY_KEY_REQUIRED', '결과 ID와 같은 요청 식별 키가 필요합니다.')
    return pipeline_services.receive(data)
