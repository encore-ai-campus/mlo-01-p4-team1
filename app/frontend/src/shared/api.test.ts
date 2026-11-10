import {describe,it,expect,vi,afterEach} from 'vitest';
import {canonical,api,setCsrf} from './api';
afterEach(()=>{vi.unstubAllGlobals();setCsrf('');});
describe('request condition equality',()=>{
  it('ignores selection ordering but detects changed groups and versions',()=>{
    expect(canonical({genre:['JAZZ','POP'],tempo:'auto'})).toBe(canonical({tempo:'auto',genre:['POP','JAZZ']}));
    expect(canonical({tempo:'slow'})).not.toBe(canonical({tempo:'auto'}));
    expect(canonical({option_version:'v1'})).not.toBe(canonical({option_version:'v2'}));
  });
});
it('sends a delivery proof separately from the event JSON while keeping CSRF and request options',async()=>{
  vi.stubGlobal('document',{cookie:'csrftoken=fixture-csrf'});
  const fetch=vi.fn().mockResolvedValue({ok:true,json:async()=>({counted:true})});
  vi.stubGlobal('fetch',fetch);
  const controller=new AbortController(),body={delivery_id:'d1',event_id:'e1'};
  await api('/api/ad-events','POST',body,'request-key',controller.signal,{'X-Ad-Delivery-Token':'proof-d1'});
  expect(fetch).toHaveBeenCalledWith('/api/ad-events',expect.objectContaining({
    credentials:'same-origin',cache:'no-store',signal:expect.any(AbortSignal),body:JSON.stringify(body),
    headers:{'Content-Type':'application/json','X-CSRFToken':'fixture-csrf','Idempotency-Key':'request-key','X-Ad-Delivery-Token':'proof-d1'},
  }));
});

it('preserves server retry timing on temporary advertising API errors',async()=>{
  vi.stubGlobal('document',{cookie:''});
  vi.stubGlobal('fetch',vi.fn().mockResolvedValue({
    ok:false,status:429,headers:new Headers({'Retry-After':'30'}),
    json:async()=>({code:'RATE_LIMITED',message:'temporary'}),
  }));
  await expect(api('/api/ad-events','POST',{})).rejects.toMatchObject({status:429,retryAfterMs:30000,code:'RATE_LIMITED'});
});
