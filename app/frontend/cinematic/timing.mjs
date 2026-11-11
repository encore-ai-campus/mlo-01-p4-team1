/** RAF can report an earlier frame timestamp than synchronous initialization. */
export function safeFrameDelta(now, previous){
  if(!Number.isFinite(now)||!Number.isFinite(previous))return 0;
  return Math.max(0,Math.min((now-previous)/1000,0.12));
}
