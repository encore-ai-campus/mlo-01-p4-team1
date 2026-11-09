import {ApiError} from '../../shared/api';

export type AdEventBody=Readonly<{delivery_id:string;event_id:string;event_type:string;occurred_at:string}>;
export type AdReport={body:AdEventBody;token:string;attempts:number;next:number;created:number;done:boolean;inflight:boolean;cancelled:boolean;controller?:AbortController};
type Sender=(body:AdEventBody,token:string,signal:AbortSignal)=>Promise<unknown>;
const maxAttempts=3,lifetimeMs=300000;

export function retryableAdError(error:unknown):boolean{
  if(error instanceof Error&&error.name==='AbortError')return false;
  return !(error instanceof ApiError)||error.status===0||[408,425,429].includes(error.status)||(error.status>=500&&error.status<600);
}
export function permanentAdResponse(error:unknown):boolean{
  return error instanceof ApiError&&error.status>=400&&error.status<500&&!retryableAdError(error);
}
export function createAdReport(body:AdEventBody,token:string,now:number):AdReport{
  return {body:Object.freeze({...body}),token,attempts:0,next:0,created:now,done:false,inflight:false,cancelled:false};
}
export function cancelAdReport(report:AdReport){
  report.cancelled=true;report.done=true;report.controller?.abort();
}
export async function transmitAdReport(report:AdReport,send:Sender,clock:()=>number=()=>performance.now()){
  const now=clock();
  if(report.cancelled||report.done||report.inflight||now<report.next)return;
  if(now-report.created>=lifetimeMs||report.attempts>=maxAttempts){report.done=true;return;}
  report.inflight=true;report.attempts++;
  const controller=new AbortController();report.controller=controller;
  try{
    await send(report.body,report.token,controller.signal);
    if(!report.cancelled)report.done=true;
  }catch(error){
    if(report.cancelled||!retryableAdError(error)||report.attempts>=maxAttempts){report.done=true;return;}
    const delay=Math.max(1000*report.attempts,error instanceof ApiError?error.retryAfterMs??0:0);
    report.next=clock()+delay;
    if(report.next>=report.created+lifetimeMs)report.done=true;
  }finally{report.inflight=false;report.controller=undefined;}
}
