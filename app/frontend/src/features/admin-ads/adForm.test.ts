import {describe,it,expect} from 'vitest';
import {AdMutationIntents,kstMinute,validatedAdFields,type AdForm} from './adForm';

const draft:AdForm={title:'synthetic advertisement',asset_id:'synthetic-asset',link_url:'https://example.org/ad',placement:'top',starts_at:'2026-11-10T09:00',ends_at:'2026-11-11T09:00',priority:'1',daily_budget_tokens:'100',total_budget_tokens:'1000'};
describe('advertisement registration intents',()=>{
  it('preserves a failed request key but allocates a new key after a completed registration',()=>{
    let sequence=0;const intents=new AdMutationIntents(()=>String(++sequence));
    const body={...validatedAdFields(draft),asset_id:draft.asset_id};
    const first=intents.key('/api/admin/ads',body);
    expect(intents.key('/api/admin/ads',body)).toBe(first);
    intents.complete('/api/admin/ads',body);
    expect(intents.key('/api/admin/ads',body)).not.toBe(first);
  });
  it('treats an explicit new advertisement as a new user intent',()=>{
    let sequence=0;const intents=new AdMutationIntents(()=>String(++sequence));
    const old=intents.key('/api/admin/ads',draft);
    intents.beginNew();
    expect(intents.key('/api/admin/ads',draft)).not.toBe(old);
  });
});
describe('advertisement form payload',()=>{
  it('preserves untouched original seconds and milliseconds while editing another field',()=>{
    const original={starts_at:'2026-11-10T00:00:19.123Z',ends_at:'2026-11-11T00:00:59.987Z'};
    const form={...draft,title:'changed title',starts_at:kstMinute(original.starts_at),ends_at:kstMinute(original.ends_at)};
    const payload=validatedAdFields(form,original);
    expect(payload.starts_at).toBe(original.starts_at);expect(payload.ends_at).toBe(original.ends_at);
  });
  it('uses the explicit selected time when changed and then restored to its displayed minute',()=>{
    const original={starts_at:'2026-11-10T00:00:19.123Z',ends_at:'2026-11-11T00:00:59.987Z'};
    const payload=validatedAdFields(draft,original,{starts_at:true,ends_at:false});
    expect(payload.starts_at).toBe('2026-11-10T00:00:00.000Z');expect(payload.ends_at).toBe(original.ends_at);
  });
  it.each(['9007199254740993','1.5','-1','0'])('rejects an unsafe or invalid budget %s before it can be uploaded or rounded',value=>{
    expect(()=>validatedAdFields({...draft,daily_budget_tokens:value,total_budget_tokens:value})).toThrow('예산');
  });
  it('retains the highest exact integer instead of silently rounding it',()=>{
    expect(validatedAdFields({...draft,daily_budget_tokens:'9007199254740991',total_budget_tokens:'9007199254740991'}).daily_budget_tokens).toBe(9007199254740991);
  });
  it('allows omitted budgets but rejects an incomplete budget pair',()=>{
    expect(validatedAdFields({...draft,daily_budget_tokens:'',total_budget_tokens:''})).not.toHaveProperty('daily_budget_tokens');
    expect(()=>validatedAdFields({...draft,daily_budget_tokens:'',total_budget_tokens:'100'})).toThrow('함께');
  });
  it.each([
    {link_url:'http://example.org'},
    {link_url:'https://user:password@example.org'},
    {link_url:'https://example.org/ with-space'},
    {starts_at:'2026-02-30T09:00'},
    {ends_at:'2026-11-01T09:00'},
  ])('rejects an invalid destination or schedule %j',invalid=>{
    expect(()=>validatedAdFields({...draft,...invalid})).toThrow();
  });
});
