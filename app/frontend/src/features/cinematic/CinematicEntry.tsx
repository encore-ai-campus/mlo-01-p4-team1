import {useCallback,useEffect,useMemo,useRef,useState,type ReactNode} from 'react';
import template from './template.html?raw';
import {SCENE_ASSET,STYLE_ASSET} from './generated';
import {escapeAttribute,hasSeenIntro,INTRO_ENTER,isIntroMessage,platformDestination,rememberIntro,shouldShowIntro} from './entry';

function tabStorage(): Storage | null {
  try { return window.sessionStorage; } catch { return null; }
}

/** Isolate WebGL styles/audio in a first-party frame; mount the existing app unchanged on entry. */
export function CinematicEntry({children}: {children: ReactNode}) {
  const [visible,setVisible]=useState(()=>shouldShowIntro(location.pathname,location.search,hasSeenIntro(tabStorage())));
  const [ready,setReady]=useState(false);
  const [slow,setSlow]=useState(false);
  const frame=useRef<HTMLIFrameElement>(null);
  const destination=useMemo(()=>platformDestination(location.search,location.hash),[]);
  const documentHtml=useMemo(()=>template
    .replaceAll('__CINEMATIC_STYLE__',escapeAttribute(STYLE_ASSET))
    .replaceAll('__CINEMATIC_SCRIPT__',escapeAttribute(SCENE_ASSET))
    .replaceAll('__PLATFORM_DESTINATION__',escapeAttribute(destination)),[destination]);

  const enter=useCallback(()=>{
    const stored=rememberIntro(tabStorage());
    // Keep a presentation-only bypass in the URL if the browser disables sessionStorage.
    history.replaceState(history.state,'',platformDestination(location.search,location.hash,!stored));
    setVisible(false);
    requestAnimationFrame(()=>{
      const heading=document.querySelector<HTMLElement>('main h1');
      if(heading){heading.setAttribute('tabindex','-1');heading.focus({preventScroll:true});}
      if(location.hash==='#services')document.getElementById('services')?.scrollIntoView({behavior:'auto'});
    });
  },[]);

  useEffect(()=>{
    if(!visible){
      // Direct login/signup/recovery links remain direct, including their next return to /platform.
      const stored=rememberIntro(tabStorage());
      if(stored&&new URLSearchParams(location.search).get('cinematic')==='skip'&&
          ['/platform','/platform/','/platform/intro','/platform/intro/'].includes(location.pathname)){
        history.replaceState(history.state,'',platformDestination(location.search,location.hash,false));
      }
      return;
    }
    const oldTitle=document.title;
    document.title='메인 플랫폼 · 사계절 인트로';
    const receive=(event: MessageEvent)=>{
      if(event.origin!==location.origin||event.source!==frame.current?.contentWindow||!isIntroMessage(event.data))return;
      if(event.data.type===INTRO_ENTER)enter();else setReady(true);
    };
    window.addEventListener('message',receive);
    return()=>{window.removeEventListener('message',receive);document.title=oldTitle;};
  },[visible,enter]);
  useEffect(()=>{
    if(!visible||ready)return;
    const timer=window.setTimeout(()=>setSlow(true),15000);
    return()=>clearTimeout(timer);
  },[visible,ready]);

  if(!visible)return <>{children}</>;
  return <section aria-label="메인 플랫폼 시네마틱 진입 화면"
    style={{position:'fixed',inset:0,zIndex:10000,background:'#080d18',color:'#f4f1e9'}}>
    <iframe data-cinematic-frame ref={frame} title="봄·여름·가을·겨울 시네마틱 인트로" srcDoc={documentHtml}
      referrerPolicy="no-referrer" allow="autoplay"
      style={{display:'block',border:0,width:'100%',height:'100%'}}/>
    {slow&&!ready&&<div role="status" style={{position:'absolute',bottom:24,left:'50%',transform:'translateX(-50%)',
      maxWidth:'90%',padding:'12px 20px',borderRadius:12,background:'#111827',textAlign:'center'}}>
      <p style={{margin:'0 0 8px'}}>3D 장면을 불러오는 데 시간이 걸립니다.</p>
      <button onClick={enter} style={{padding:'9px 16px',cursor:'pointer'}}>메인 플랫폼으로 바로 이동 →</button>
    </div>}
  </section>;
}
