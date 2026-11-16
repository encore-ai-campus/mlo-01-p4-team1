"""Submit a bounded playlist proposal request to OpenAI."""
import hashlib
from heapq import nsmallest
import json
import os
from fractions import Fraction
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings

from .errors import require, DomainError
from .policy import MINIMUM, MAXIMUM

OPENAI_PLAYLIST_MODEL = 'gpt-6-luna'
OPENAI_RESPONSES_URL = 'https://api.openai.com/v1/responses'
MAX_RESPONSE_BYTES = 1024 * 1024
PROPOSAL_SCHEMA = {'type':'object','properties':{
    'track_ids':{'type':'array','items':{'type':'string'}},'summary':{'type':'string'},
    'track_reasons':{'type':'array','items':{'type':'object','properties':{'track_id':{'type':'string'},'reason':{'type':'string'}},
        'required':['track_id','reason'],'additionalProperties':False}}},
    'required':['track_ids','summary','track_reasons'],'additionalProperties':False}
COMPACT_PROPOSAL_SCHEMA = {
    'type': 'object',
    'properties': {'track_ids': {'type': 'array', 'items': {'type': 'integer'}}, 'summary': {'type': 'string'}},
    'required': ['track_ids', 'summary'],
    'additionalProperties': False,
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def candidate_window(candidates, context, parent=None):
    """Bound the API input, preserving genre representatives and fresh choices.

    This window is not evidence that the whole catalog is insufficient.
    """
    from .solver import representatives
    limit=settings.OPENAI_PLAYLIST_MAX_CANDIDATES
    require(limit>0,'RECONFIGURATION_UNAVAILABLE','GPT 선곡 후보 한도 설정이 필요해요.',503)
    seed=context['seed']; off=context['options']['weather_influence_mode']=='OFF'
    def rank(track):
        tie=hashlib.sha256((seed+'\0'+track['track_id']).encode()).digest() if off else b''
        return -Fraction(track['scores']['S'] or 0),tie,track['track_id']
    selected=context['options']['selected_genre_codes']
    if not selected and parent is None:
        return nsmallest(limit, candidates, key=rank)
    ordered=sorted(candidates,key=rank)
    # Reserve selected-genre material before optional complementary genres.
    if off and selected:
        ordered.sort(key=lambda t:0 if set(t['genre_group_codes']) & set(selected) else 1)
    assigned=representatives(ordered,selected) if selected else {}
    require(assigned is not None,'SEARCH_INCOMPLETE','선택 장르의 서로 다른 대표 곡을 확인하지 못했어요.',503)
    reserved=set(assigned.values())
    if parent is not None:
        previous = set(parent)
        needed = max(1, len(previous) // 5)
        fresh = [t for t in ordered if t['track_id'] not in previous and t['track_id'] not in reserved]
        reserved.update(t['track_id'] for t in fresh[:max(0, needed - len(reserved - previous))])
        require(len(reserved - previous) >= needed, 'NO_MEANINGFUL_CHANGE',
                '필요한 신규 곡 수를 채우지 못했어요. 현재 선곡표는 유지됩니다.',
                parent_track_count=len(previous), required_new_count=needed, actual_new_count=len(reserved - previous))
    require(len(reserved)<=limit,'SEARCH_INCOMPLETE','GPT 후보 한도로 필수 후보를 전달할 수 없어요.',503)
    window=[t for t in ordered if t['track_id'] in reserved]
    window.extend(t for t in ordered if t['track_id'] not in reserved)
    return sorted(window[:limit],key=rank)


def _request_openai(payload, api_key, timeout_ms):
    try:
        request = Request(OPENAI_RESPONSES_URL, method='POST',
                          data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8'),
                          headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json',
                                   'Accept': 'application/json'})
        with build_opener(NoRedirect()).open(request, timeout=timeout_ms / 1000) as response:
            return response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        if status in (429, 503):
            raise DomainError('PROVIDER_BUSY', 'GPT 선곡 요청이 몰렸어요. 잠시 후 다시 요청해 주세요.', 503,
                              {'provider_status': status}) from None
        raise DomainError('PROVIDER_UNAVAILABLE', 'GPT 선곡 서비스에 연결하지 못했어요.', 503) from None
    except (URLError, OSError, HTTPException, ValueError):
        raise DomainError('PROVIDER_UNAVAILABLE', 'GPT 선곡 서비스에 연결하지 못했어요.', 503) from None


def _parse_openai_response(raw):
    invalid = 'GPT 선곡 응답을 확인할 수 없어요.'
    require(len(raw) <= MAX_RESPONSE_BYTES, 'PROVIDER_RESPONSE_INVALID', invalid)
    try:
        response = json.loads(raw)
        require(type(response) is dict and response.get('status') == 'completed'
                and type(response.get('output')) is list, 'PROVIDER_RESPONSE_INVALID', invalid)
        texts = []
        for item in response['output']:
            require(type(item) is dict, 'PROVIDER_RESPONSE_INVALID', invalid)
            if item.get('type') == 'reasoning':
                continue
            require(item.get('type') == 'message' and item.get('role') == 'assistant'
                    and item.get('status') == 'completed' and type(item.get('content')) is list,
                    'PROVIDER_RESPONSE_INVALID', invalid)
            for content in item['content']:
                require(type(content) is dict and content.get('type') == 'output_text'
                        and type(content.get('text')) is str, 'PROVIDER_RESPONSE_INVALID', invalid)
                texts.append(content['text'])
        proposal = json.loads(''.join(texts))
        require(type(proposal) is dict, 'PROVIDER_RESPONSE_INVALID', invalid)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise DomainError('PROVIDER_RESPONSE_INVALID', invalid) from None
    usage = response.get('usage')
    usage = usage if type(usage) is dict else {}
    return proposal, {key: usage[key] if type(usage.get(key)) is int and usage[key] >= 0 else None
                      for key in ('input_tokens', 'output_tokens')}


def propose(candidates, context, parent=None, *, compact=False):
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    timeout_ms = settings.OPENAI_PLAYLIST_TIMEOUT_MS
    token_budget = settings.OPENAI_PLAYLIST_MAX_OUTPUT_TOKENS
    require(bool(api_key) and timeout_ms > 0 and token_budget > 0 and settings.OPENAI_PLAYLIST_MAX_CANDIDATES > 0,
            'RECONFIGURATION_UNAVAILABLE', 'GPT 선곡 API 설정을 확인해 주세요.', 503)
    require(len(candidates) <= settings.OPENAI_PLAYLIST_MAX_CANDIDATES,
            'SEARCH_INCOMPLETE', 'GPT 선곡 입력 후보 한도를 확인해 주세요.', 503)
    require(sum(track['duration_ms'] for track in candidates) >= MINIMUM,
            'SEARCH_INCOMPLETE', '전달 가능한 후보에서 선곡 시간을 채우지 못했어요.', 503)

    options = context['options']
    mode = options['weather_influence_mode']
    selected = options['selected_genre_codes']
    trigger = context.get('recommendation_trigger')
    if trigger not in ('initial', 'regeneration', 'weather_refresh'):
        trigger = 'initial' if parent is None else 'regeneration'
    track_ids = [track['track_id'] for track in candidates]
    aliases = {track_id: index for index, track_id in enumerate(track_ids)} if compact else {}
    instructions = [
        f'Choose only supplied track IDs, without duplicates, for a {MINIMUM // 60000}–{MAXIMUM // 60000} minute playlist.',
        'Order adjacent tracks for a natural transition while respecting the supplied preferences and weather mode.',
        'Return a concise Korean summary.' if compact
        else 'Return a concise Korean summary and exactly one short Korean reason per selected track.',
        'Use only the supplied information. Do not invent a track, audio feature, or weather condition.',
    ]
    if selected:
        instructions.append('Each selected genre needs a different representative track.')
        instructions.append('Use only selected-genre tracks if feasible; otherwise selected genres must cover at least 80% of playback time.'
                            if mode == 'OFF' else 'Every track must match at least one selected genre.')
    data = {
        'trigger': trigger,
        'weather_mode': mode,
        'weather_code': context.get('weather_code') if mode == 'ON' else None,
        'selected_genres': selected,
        'selected_themes': options.get('selected_theme_codes', []),
        'vocal_option': options.get('vocal_option'),
        'candidates': [{
            'track_id': aliases.get(track['track_id'], track['track_id']), 'duration_ms': track['duration_ms'],
            'genre_group_codes': track.get('genre_group_codes', []), 'theme_codes': track.get('theme_codes', []),
            'scores': {key: float(value) if value is not None else None
                       for key, value in track.get('scores', {}).items() if key in ('W', 'P', 'S')},
        } for track in candidates],
    }
    if parent is not None:
        previous = list(dict.fromkeys(parent))
        data['parent_track_ids'] = [aliases[track_id] for track_id in previous if track_id in aliases] if compact else previous
        instructions.append(f'Choose at least {max(1, len(previous) // 5)} track IDs absent from the parent playlist.')
    format_name = 'playlist_compact_v1' if compact else 'playlist_proposal_v1'
    payload = {
        'model': OPENAI_PLAYLIST_MODEL, 'store': False, 'instructions': ' '.join(instructions),
        'input': json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':') if compact else None),
        'reasoning': {'effort': 'low'}, 'max_output_tokens': token_budget,
        'text': {'format': {'type': 'json_schema', 'name': format_name,
                            'strict': True, 'schema': COMPACT_PROPOSAL_SCHEMA if compact else PROPOSAL_SCHEMA}},
    }
    proposal, usage = _parse_openai_response(_request_openai(payload, api_key, timeout_ms))
    if compact:
        selected_ids = proposal.get('track_ids')
        require(set(proposal) == {'track_ids', 'summary'} and type(selected_ids) is list
                and all(type(index) is int and 0 <= index < len(track_ids) for index in selected_ids),
                'PROVIDER_RESPONSE_INVALID', 'GPT 선곡 응답 검증에 실패했어요.')
        proposal = {'track_ids': [track_ids[index] for index in selected_ids],
                    'summary': proposal['summary'], 'track_reasons': []}
    return proposal, {
        'model': OPENAI_PLAYLIST_MODEL, 'prompt_version': 'c-gpt-playlist-compact-v1' if compact else 'c-gpt-playlist-v1',
        'schema_version': 'OpenAIPlaylistCompact-v1' if compact else 'OpenAIPlaylistProposal-v1', 'attempts': 1,
        'candidate_ids_sha256': hashlib.sha256(json.dumps(track_ids).encode()).hexdigest(),
        **usage,
    }


def validate_proposal(proposal, candidates, parent, options, all_candidates=None, *, compact=False):
    fail = lambda condition: require(condition,'PROVIDER_RESPONSE_INVALID','GPT 선곡 응답 검증에 실패했어요.')
    fail(type(proposal) is dict and set(proposal)=={'track_ids','summary','track_reasons'})
    ids, reasons = proposal['track_ids'], proposal['track_reasons']
    fail(type(ids) is list and len(ids)>0 and all(type(x) is str for x in ids))
    fail(len(ids)==len(set(ids)))
    lookup = {t['track_id']:t for t in candidates}
    fail(set(ids)<=lookup.keys() and type(proposal['summary']) is str and type(reasons) is list)
    fail(all(type(r) is dict and set(r)=={'track_id','reason'} and type(r['track_id']) is str
             and type(r['reason']) is str and bool(r['reason'].strip()) for r in reasons))
    fail(not reasons if compact else len(reasons)==len(ids) and {r['track_id'] for r in reasons}==set(ids))
    tracks = [lookup[x] for x in ids]
    fail(MINIMUM <= sum(t['duration_ms'] for t in tracks) <= MAXIMUM)
    selected=options['selected_genre_codes']
    if selected:
        from .solver import representatives
        fail(representatives(tracks,selected) is not None)
        if options['weather_influence_mode']=='ON':
            fail(all(set(track['genre_group_codes']) & set(selected) for track in tracks))
        else:
            total=sum(t['duration_ms'] for t in tracks)
            selected_duration=sum(t['duration_ms'] for t in tracks if set(t['genre_group_codes']) & set(selected))
            fail(5*selected_duration >= 4*total)
            if selected_duration < total:
                from .solver import choose
                selected_candidates=[t for t in (all_candidates if all_candidates is not None else candidates)
                                     if set(t['genre_group_codes']) & set(selected)]
                try:
                    choose(selected_candidates,0,settings.SEARCH_SECONDS,'ON',selected)
                except DomainError as exc:
                    if exc.code!='CANDIDATE_SHORTAGE': raise
                else:
                    fail(False)
    if parent is not None:
        previous = set(parent)
        needed, actual = max(1, len(previous) // 5), len(set(ids) - previous)
        require(actual>=needed,'NO_MEANINGFUL_CHANGE',
                '기존 선곡표와 다른 곡을 찾지 못했어요. 현재 선곡표는 유지됩니다.' if actual==0 else '필요한 신규 곡 수를 채우지 못했어요. 현재 선곡표는 유지됩니다.',
                parent_track_count=len(previous),required_new_count=needed,actual_new_count=actual)
    return tracks
