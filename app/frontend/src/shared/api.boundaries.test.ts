import {afterEach,describe,expect,it,vi} from 'vitest';
import {api,csrfHeaders,requestJson,setCsrf} from './api';

afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals();setCsrf('');});
describe('shared API request boundaries',()=>{
  it('uses a supplied masked CSRF token and supports both cookie names on a fresh page',()=>{
    vi.stubGlobal('document',{cookie:'platform_csrf=platform-fixture'});
    expect(csrfHeaders()).toEqual({'X-CSRFToken':'platform-fixture'});
    setCsrf('masked-fixture');expect(csrfHeaders()).toEqual({'X-CSRFToken':'masked-fixture'});
    setCsrf('');vi.stubGlobal('document',{cookie:'csrftoken=legacy-fixture'});
    expect(csrfHeaders()).toEqual({'X-CSRFToken':'legacy-fixture'});
  });
  it('posts platform-only CSRF with JSON and multipart through the same header source',async()=>{
    vi.stubGlobal('document',{cookie:'platform_csrf=platform-fixture'});
    const fetch=vi.fn().mockResolvedValue({ok:true,status:201,json:async()=>({asset_id:'synthetic-asset'})});
    vi.stubGlobal('fetch',fetch);
    await api('/api/admin/ads','POST',{title:'synthetic'});
    const body=new FormData();body.append('image',new Blob(['synthetic']));
    await requestJson('/api/admin/ad-assets',{method:'POST',headers:csrfHeaders(),body});
    for(const call of fetch.mock.calls)expect(call[1].headers['X-CSRFToken']).toBe('platform-fixture');
    expect(fetch.mock.calls[1][1].headers).not.toHaveProperty('Content-Type');
  });
  it('times out and aborts a fetch that never resolves even if the mock ignores abort',async()=>{
    vi.useFakeTimers();let signal:AbortSignal|undefined;
    vi.stubGlobal('fetch',vi.fn((_path,init)=>{signal=init.signal;return new Promise(()=>{});}));
    const request=requestJson('/api/admin/ads/analytics',{},100);
    const failure=expect(request).rejects.toMatchObject({code:'REQUEST_TIMEOUT'});
    await vi.advanceTimersByTimeAsync(100);await failure;
    expect(signal?.aborted).toBe(true);
  });
  it('keeps the timeout active until the response body has finished parsing',async()=>{
    vi.useFakeTimers();let signal:AbortSignal|undefined;
    vi.stubGlobal('fetch',vi.fn(async(_path,init)=>{signal=init.signal;return {ok:true,status:200,json:()=>new Promise(()=>{})};}));
    const request=requestJson('/api/admin/ads/analytics',{},100);
    const failure=expect(request).rejects.toMatchObject({code:'REQUEST_TIMEOUT'});
    await vi.advanceTimersByTimeAsync(100);await failure;expect(signal?.aborted).toBe(true);
  });
  it('propagates caller cancellation immediately instead of waiting for a hung fetch',async()=>{
    const controller=new AbortController();let signal:AbortSignal|undefined;
    vi.stubGlobal('fetch',vi.fn((_path,init)=>{signal=init.signal;return new Promise(()=>{});}));
    const request=requestJson('/api/admin/ads/analytics',{signal:controller.signal});
    const failure=expect(request).rejects.toMatchObject({name:'AbortError'});
    controller.abort();await failure;expect(signal?.aborted).toBe(true);
  });
  it.each([null,'not-an-object',42])('rejects an unexpected successful JSON value %j',async value=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,status:200,json:async()=>value}));
    await expect(requestJson('/api/admin/ads/analytics')).rejects.toMatchObject({code:'INVALID_RESPONSE'});
  });
  it('reports a successful non-JSON body as an error',async()=>{
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,status:200,json:async()=>{throw new SyntaxError('synthetic HTML');}}));
    await expect(requestJson('/api/admin/ads/analytics')).rejects.toMatchObject({code:'INVALID_RESPONSE'});
  });
  it('supports a legitimate 204 response without trying to parse JSON',async()=>{
    const json=vi.fn();
    vi.stubGlobal('fetch',vi.fn().mockResolvedValue({ok:true,status:204,json}));
    await expect(requestJson('/api/logout',{method:'POST'})).resolves.toBeUndefined();
    expect(json).not.toHaveBeenCalled();
  });
});
