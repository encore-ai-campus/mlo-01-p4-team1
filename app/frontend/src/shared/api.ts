let csrf = '';
export function setCsrf(value:string){csrf=value;}
export function csrfHeaders():Record<string,string>{
  const cookies=typeof document==='undefined'?[]:document.cookie.split(';').map(value=>value.trim());
  const token=csrf||cookies.find(value=>value.startsWith('platform_csrf='))?.slice('platform_csrf='.length)||cookies.find(value=>value.startsWith('csrftoken='))?.slice('csrftoken='.length)||'';
  return token?{'X-CSRFToken':token}:{};
}
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
export const API_REQUEST_TIMEOUT_MS=15000;
export async function requestJson<T>(path:string,init:RequestInit={},timeoutMs=API_REQUEST_TIMEOUT_MS):Promise<T>{
  if(!Number.isSafeInteger(timeoutMs)||timeoutMs<=0)throw new RangeError('Invalid request timeout');
  const external=init.signal;
  if(external?.aborted)throw new DOMException('Aborted','AbortError');
  const controller=new AbortController();
  let timedOut=false,timer:ReturnType<typeof setTimeout>|undefined,onAbort:(()=>void)|undefined;
  const interrupted=new Promise<never>((_resolve,reject)=>{
    onAbort=()=>{controller.abort();reject(new DOMException('Aborted','AbortError'));};
    external?.addEventListener('abort',onAbort,{once:true});
    timer=setTimeout(()=>{timedOut=true;controller.abort();reject(new ApiError('REQUEST_TIMEOUT','응답이 지연되고 있습니다. 다시 시도해 주세요.'));},timeoutMs);
  });
  const operation=(async()=>{
    const response=await fetch(path,{...init,signal:controller.signal});
    if(response.status===204)return undefined as T;
    let json:unknown;
    try{json=await response.json();}
    catch{
      if(response.ok)throw new ApiError('INVALID_RESPONSE','서버 응답 형식을 확인해 주세요.',{},response.status);
      json=null;
    }
    if(!response.ok){
      const error=json&&typeof json==='object'?json as {code?:string;message?:string;details?:Record<string,unknown>}:null;
      throw new ApiError(error?.code||'SERVICE_UNAVAILABLE',error?.message||'서비스 연결을 확인해 주세요.',error?.details,response.status,retryAfterMilliseconds(response.headers?.get?.('Retry-After')??null));
    }
    if(json===null||typeof json!=='object')throw new ApiError('INVALID_RESPONSE','서버 응답 형식을 확인해 주세요.',{},response.status);
    return json as T;
  })();
  try{return await Promise.race([operation,interrupted]);}
  catch(error){
    if(timedOut)throw new ApiError('REQUEST_TIMEOUT','응답이 지연되고 있습니다. 다시 시도해 주세요.');
    if(external?.aborted)throw new DOMException('Aborted','AbortError');
    throw error;
  }finally{
    if(timer!==undefined)clearTimeout(timer);
    if(onAbort)external?.removeEventListener('abort',onAbort);
  }
}
export async function api<T>(path:string,method='GET',body?:unknown,idempotencyKey?:string,signal?:AbortSignal,headers:Record<string,string>={}):Promise<T>{
  return requestJson<T>(path,{method,credentials:'same-origin',cache:'no-store',signal,
    headers:{...headers,'Content-Type':'application/json',...csrfHeaders(),...(idempotencyKey?{'Idempotency-Key':idempotencyKey}:{})},
    body:body===undefined?undefined:JSON.stringify(body)});
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
