let csrf = '';
export function setCsrf(value:string){csrf=value;}
export class ApiError extends Error {
  constructor(public code:string,message:string,public details:Record<string,unknown>={},public status=0,public retryAfterMs:number|null=null){super(message);}
}
export function retryAfterMilliseconds(value:string|null,now=Date.now()):number|null{
  if(value===null||!value.trim())return null;
  const input=value.trim();
  if(/^\d+$/.test(input)){const delay=Number(input)*1000;return Number.isSafeInteger(delay)?delay:null;}
  const when=Date.parse(input);return Number.isFinite(when)?Math.max(0,when-now):null;
}
export const key = () => {
  // getRandomValues also exists on the LAN HTTP origin used for synthetic demos.
  if(typeof crypto.randomUUID==='function')return crypto.randomUUID();
  const bytes=crypto.getRandomValues(new Uint8Array(16));
  bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
  const hex=Array.from(bytes,value=>value.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
};
export async function api<T>(path:string,method='GET',body?:unknown,idempotencyKey?:string,signal?:AbortSignal,headers:Record<string,string>={}):Promise<T>{
  const token=csrf || document.cookie.split('; ').find(x=>x.startsWith('csrftoken='))?.slice(10) || '';
  const response=await fetch(path,{method,credentials:'same-origin',cache:'no-store',signal,
    headers:{...headers,'Content-Type':'application/json',...(token?{'X-CSRFToken':token}:{}),...(idempotencyKey?{'Idempotency-Key':idempotencyKey}:{})},
    body:body===undefined?undefined:JSON.stringify(body)});
  const json=await response.json().catch(()=>null);
  if(!response.ok)throw new ApiError(json?.code || 'SERVICE_UNAVAILABLE',json?.message || '서비스 연결을 확인해 주세요.',json?.details,response.status,retryAfterMilliseconds(response.headers?.get?.('Retry-After')??null));
  return json;
}
export const wait=(ms:number,signal:AbortSignal)=>new Promise<void>((resolve,reject)=>{
  if(signal.aborted){reject(new DOMException('Aborted','AbortError'));return;}
  const onAbort=()=>{clearTimeout(timer);reject(new DOMException('Aborted','AbortError'));};
  const timer=setTimeout(()=>{signal.removeEventListener('abort',onAbort);resolve();},ms);
  signal.addEventListener('abort',onAbort,{once:true});
});
export function canonical(value:unknown):string {
  if(Array.isArray(value))return JSON.stringify([...value].sort());
  if(value&&typeof value==='object')return JSON.stringify(Object.fromEntries(Object.entries(value).sort(([a],[b])=>a.localeCompare(b)).map(([k,v])=>[k,canonical(v)])));
  return JSON.stringify(value);
}
