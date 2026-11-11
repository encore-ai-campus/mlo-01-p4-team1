export class Phase4Error extends Error {
  constructor(public readonly code: string, message: string, public readonly status = 0,
    public readonly details: Record<string, unknown> = {}) {super(message);}
}

/** Object keys are canonical; array order remains meaningful (e.g. requested songs). */
export function canonical(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  return `{${Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b))
    .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(',')}}`;
}

export class MutationKeys {
  private readonly pending = new Map<string, string>();
  constructor(private readonly create: () => string = () => crypto.randomUUID()) {}
  private signature(path: string, body: unknown) {return `${path}\n${canonical(body)}`;}
  key(path: string, body: unknown) {
    const signature = this.signature(path, body);
    let key = this.pending.get(signature);
    if (!key) {key = this.create(); this.pending.set(signature, key);}
    return key;
  }
  completed(path: string, body: unknown) {this.pending.delete(this.signature(path, body));}
}

export function cookieCsrf(): string {
  if (typeof document === 'undefined') return '';
  const raw = document.cookie.split(';').map(value => value.trim()).find(value => value.startsWith('csrftoken='))?.slice(10);
  if (!raw) return '';
  try {return decodeURIComponent(raw);} catch {return '';}
}

export interface Transport {
  get<T>(path: string, signal?: AbortSignal): Promise<T>;
  post<T>(path: string, body: unknown, idempotencyKey: string, signal?: AbortSignal): Promise<T>;
}

export function createTransport(options: {
  base?: string; fetch?: typeof fetch; csrf?: () => string;
} = {}): Transport {
  const base = (options.base || '/api/phase4').replace(/\/$/, '');
  if (!/^\/[A-Za-z0-9/_-]+$/.test(base)) throw new Error('API 기본 경로는 같은 출처의 절대 경로여야 합니다.');
  const send = options.fetch || globalThis.fetch.bind(globalThis);
  async function request<T>(path: string, method: 'GET' | 'POST', body?: unknown, key?: string, signal?: AbortSignal): Promise<T> {
    if (!path.startsWith('/') || path.startsWith('//') || path.split('?')[0].split('/').includes('..') || /[\\#\r\n]/.test(path)) {
      throw new Phase4Error('INVALID_PATH', '요청 경로를 확인해 주세요.');
    }
    const token = method === 'POST' ? (options.csrf || cookieCsrf)() : '';
    if (method === 'POST' && !token) throw new Phase4Error('CSRF_REQUIRED', '보안 토큰을 확인해 주세요. 새로고침 후 다시 시도해 주세요.');
    if (method === 'POST' && !key) throw new Phase4Error('IDEMPOTENCY_REQUIRED', '요청 식별자를 확인해 주세요.');
    let response: Response;
    try {
      response = await send(base + path, {method, credentials: 'same-origin', cache: 'no-store', signal,
        headers: {'Accept': 'application/json', ...(method === 'POST' ? {'Content-Type': 'application/json', 'X-CSRFToken': token, 'Idempotency-Key': key!} : {})},
        ...(method === 'POST' ? {body: JSON.stringify(body)} : {})});
    } catch (error) {
      if (signal?.aborted || (error instanceof Error && error.name === 'AbortError')) throw error;
      throw new Phase4Error('NETWORK_ERROR', '전송 결과를 확인하지 못했습니다. 같은 내용으로 다시 시도해 주세요.');
    }
    const payload = await response.json().catch(() => null) as Record<string, unknown> | null;
    if (!response.ok) {
      const error = payload?.error && typeof payload.error === 'object' ? payload.error as Record<string, unknown> : payload;
      throw new Phase4Error(typeof error?.code === 'string' ? error.code : 'SERVICE_UNAVAILABLE',
        typeof error?.message === 'string' ? error.message : '서비스 연결을 확인해 주세요.', response.status,
        error?.details && typeof error.details === 'object' ? error.details as Record<string, unknown> : {});
    }
    if (payload === null || typeof payload !== 'object') throw new Phase4Error('INVALID_RESPONSE', '서버 응답을 확인할 수 없습니다.', response.status);
    return payload as T;
  }
  return {get: (path, signal) => request(path, 'GET', undefined, undefined, signal),
    post: (path, body, key, signal) => request(path, 'POST', body, key, signal)};
}
