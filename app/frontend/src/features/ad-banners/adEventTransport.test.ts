import {describe,it,expect,vi,afterEach} from 'vitest';
import {ApiError,retryAfterMilliseconds} from '../../shared/api';
import {AD_ATTEMPT_TIMEOUT_MS,createAdReport,transmitAdReport,cancelAdReport,permanentAdResponse} from './adEventTransport';

afterEach(()=>vi.useRealTimers());
const body={delivery_id:'delivery-test',event_id:'event-test',event_type:'CLICK',occurred_at:'2026-11-09T01:00:00Z'};
describe('advertising event transport',()=>{
  it.each([408,425,429,500,503])('retries temporary HTTP %s with the original immutable event and proof',async status=>{
    const report=createAdReport(body,'synthetic-proof',100);
    let now=100;
    const send=vi.fn().mockRejectedValueOnce(new ApiError('TEMPORARY','temporary',{},status)).mockResolvedValue({});
    await transmitAdReport(report,send,()=>now);
    expect(report.done).toBe(false);
    await transmitAdReport(report,send,()=>now);
    expect(send).toHaveBeenCalledTimes(1);
    now=report.next;await transmitAdReport(report,send,()=>now);
    expect(report.done).toBe(true);expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls[0][0]).toBe(send.mock.calls[1][0]);
    expect(send.mock.calls[1][0]).toEqual(body);expect(Object.isFrozen(report.body)).toBe(true);
    expect(send.mock.calls[1][1]).toBe('synthetic-proof');
  });
  it.each([400,401,403,404,409,422])('stops permanent HTTP %s instead of creating another event',async status=>{
    const report=createAdReport(body,'synthetic-proof',0);
    const send=vi.fn().mockRejectedValue(new ApiError('REJECTED','rejected',{},status));
    await transmitAdReport(report,send,()=>0);await transmitAdReport(report,send,()=>10000);
    expect(report.done).toBe(true);expect(send).toHaveBeenCalledTimes(1);
    expect(permanentAdResponse(new ApiError('REJECTED','',{},status))).toBe(true);
  });
  it('honors Retry-After and does not retry before that time',async()=>{
    const report=createAdReport(body,'synthetic-proof',10);
    const send=vi.fn().mockRejectedValue(new ApiError('RATE_LIMITED','',{},429,15000));
    await transmitAdReport(report,send,()=>10);
    expect(report.next).toBe(15010);
    await transmitAdReport(report,send,()=>15009);expect(send).toHaveBeenCalledTimes(1);
  });
  it('stops after the same event has exhausted three attempts',async()=>{
    const report=createAdReport(body,'synthetic-proof',0);
    const send=vi.fn().mockRejectedValue(new TypeError('network'));
    let now=0;
    for(let i=0;i<4;i++){await transmitAdReport(report,send,()=>now);now=report.next;}
    expect(report.done).toBe(true);expect(send).toHaveBeenCalledTimes(3);
  });
  it('does not send an expired event or retry past its lifetime',async()=>{
    const expired=createAdReport(body,'synthetic-proof',0),send=vi.fn();
    await transmitAdReport(expired,send,()=>300000);
    expect(send).not.toHaveBeenCalled();expect(expired.done).toBe(true);
    const waiting=createAdReport(body,'synthetic-proof',0);
    send.mockRejectedValue(new ApiError('RATE_LIMITED','',{},429,400000));
    await transmitAdReport(waiting,send,()=>0);expect(waiting.done).toBe(true);
  });
  it('prevents concurrent duplicate posts and aborts a cancelled report',async()=>{
    const report=createAdReport(body,'synthetic-proof',0);
    let signal:AbortSignal|undefined;
    const send=vi.fn((_body,_token,value:AbortSignal)=>new Promise((_resolve,reject)=>{
      signal=value;value.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')));
    }));
    const pending=transmitAdReport(report,send,()=>0);
    await transmitAdReport(report,send,()=>0);expect(send).toHaveBeenCalledTimes(1);
    cancelAdReport(report);await pending;
    expect(signal?.aborted).toBe(true);expect(report.done).toBe(true);expect(report.inflight).toBe(false);
    await transmitAdReport(report,send,()=>1000);expect(send).toHaveBeenCalledTimes(1);
  });
  it('parses only usable Retry-After values',()=>{
    const now=Date.parse('2026-11-09T00:00:00Z');
    expect(retryAfterMilliseconds('12',now)).toBe(12000);
    expect(retryAfterMilliseconds('Mon, 09 Nov 2026 00:00:04 GMT',now)).toBe(4000);
    expect(retryAfterMilliseconds(null,now)).toBeNull();
    expect(retryAfterMilliseconds('not-a-date',now)).toBeNull();
  });
});

describe('hung advertising requests',()=>{
  it('aborts a stalled attempt and retries the same event after backoff',async()=>{
    vi.useFakeTimers();vi.setSystemTime(0);
    const report=createAdReport(body,'synthetic-proof',0);
    let firstSignal:AbortSignal|undefined;
    const send=vi.fn().mockImplementationOnce((_body,_token,signal)=>{firstSignal=signal;return new Promise(()=>{});}).mockResolvedValue({});
    const pending=transmitAdReport(report,send,()=>Date.now());
    await vi.advanceTimersByTimeAsync(AD_ATTEMPT_TIMEOUT_MS);await pending;
    expect(firstSignal?.aborted).toBe(true);expect(report.inflight).toBe(false);expect(report.done).toBe(false);
    await vi.advanceTimersByTimeAsync(1000);await transmitAdReport(report,send,()=>Date.now());
    expect(report.done).toBe(true);expect(send).toHaveBeenCalledTimes(2);
    expect(send.mock.calls[0][0]).toBe(send.mock.calls[1][0]);expect(send.mock.calls[0][1]).toBe(send.mock.calls[1][1]);
  });
  it('enforces lifetime expiry even when another request is inflight',async()=>{
    vi.useFakeTimers();let now=0,signal:AbortSignal|undefined;
    const report=createAdReport(body,'synthetic-proof',0);
    const send=vi.fn((_body,_token,value:AbortSignal)=>{signal=value;return new Promise(()=>{});});
    const pending=transmitAdReport(report,send,()=>now);
    now=300001;await transmitAdReport(report,send,()=>now);await pending;
    expect(signal?.aborted).toBe(true);expect(report.done).toBe(true);expect(report.inflight).toBe(false);expect(send).toHaveBeenCalledTimes(1);
  });
  it('retries an accepted event whose successful response body was invalid',async()=>{
    const report=createAdReport(body,'synthetic-proof',0);
    const send=vi.fn().mockRejectedValueOnce(new ApiError('INVALID_RESPONSE','synthetic',{},200)).mockResolvedValue({});
    let now=0;await transmitAdReport(report,send,()=>now);
    expect(report.done).toBe(false);now=report.next;await transmitAdReport(report,send,()=>now);
    expect(report.done).toBe(true);expect(send.mock.calls[0][0]).toBe(send.mock.calls[1][0]);
  });
});
