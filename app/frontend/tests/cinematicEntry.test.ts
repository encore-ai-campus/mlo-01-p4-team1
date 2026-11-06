import {safeFrameDelta} from '../cinematic/timing.mjs';
import {describe,expect,it} from 'vitest';
import {readFileSync,existsSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {resolve} from 'node:path';
import {INTRO_ENTER,INTRO_READY,INTRO_SEEN_KEY,escapeAttribute,hasSeenIntro,isIntroMessage,platformDestination,rememberIntro,shouldShowIntro} from '../src/features/cinematic/entry';
import {SCENE_ASSET,STYLE_ASSET} from '../src/features/cinematic/generated';

describe('cinematic entry without modifying account routes',()=>{
  it('shows the initial platform landing only once per tab',()=>{
    expect(shouldShowIntro('/platform','',false)).toBe(true);
    expect(shouldShowIntro('/platform/','',false)).toBe(true);
    expect(shouldShowIntro('/platform','',true)).toBe(false);
  });
  it.each(['/platform/login','/platform/signup','/platform/account','/platform/account/recovery','/platform/admin',
    '/platform/admin/login','/platform/review','/platform/business','/platform/consumer','/platform/terms','/platform/privacy',
    '/platform/contact','/api/platform/v1/sessions','/login','/signup','/account/recovery','/'])('never captures %s',path=>{
    expect(shouldShowIntro(path,'',false)).toBe(false);
    expect(shouldShowIntro(path,'?cinematic=replay',false)).toBe(false);
  });
  it('supports an explicit replay and a fallback home link',()=>{
    expect(shouldShowIntro('/platform','?cinematic=replay',true)).toBe(true);
    expect(shouldShowIntro('/platform/intro','',true)).toBe(true);
    expect(shouldShowIntro('/platform/intro/','',true)).toBe(true);
    expect(shouldShowIntro('/platform','?cinematic=skip',false)).toBe(false);
    expect(shouldShowIntro('/platform/intro','?cinematic=skip',false)).toBe(false);
  });
  it('stores only a presentation flag, not personal data or auth tokens',()=>{
    const values=new Map<string,string>();
    const storage={getItem:(key:string)=>values.get(key)??null,setItem:(key:string,value:string)=>{values.set(key,value);}};
    expect(hasSeenIntro(storage)).toBe(false);
    expect(rememberIntro(storage)).toBe(true);
    expect(hasSeenIntro(storage)).toBe(true);
    expect([...values]).toEqual([[INTRO_SEEN_KEY,'1']]);
  });
  it('handles disabled browser storage without trapping the user',()=>{
    const storage={getItem:()=>{throw new Error('blocked');},setItem:()=>{throw new Error('blocked');}};
    expect(hasSeenIntro(storage)).toBe(false);
    expect(rememberIntro(storage)).toBe(false);
    expect(rememberIntro(null)).toBe(false);
    const home=platformDestination('','',true);
    expect(home).toBe('/platform?cinematic=skip');
    expect(shouldShowIntro('/platform',home.slice(home.indexOf('?')),false)).toBe(false);
  });
  it('preserves ordinary parameters and the requested section, not replay loops',()=>{
    expect(platformDestination('?cinematic=replay&campaign=demo','#services')).toBe('/platform?campaign=demo&cinematic=skip#services');
    expect(platformDestination('?cinematic=replay','#services',false)).toBe('/platform#services');
    expect(platformDestination('','',false)).toBe('/platform');
    expect(platformDestination('?next=https://example.invalid','',false)).toMatch(/^\/platform\?/);
    expect(platformDestination('','javascript:alert(1)',false)).toBe('/platform');
  });
  it('validates the exact intro message shape',()=>{
    expect(isIntroMessage({type:INTRO_ENTER,version:1})).toBe(true);
    expect(isIntroMessage({type:INTRO_READY,version:1})).toBe(true);
    for(const data of [null,undefined,'enter',{}, {type:INTRO_ENTER,version:2}, {type:'authenticate',version:1}]){
      expect(isIntroMessage(data)).toBe(false);
    }
  });
  it('escapes srcDoc attribute values',()=>{
    expect(escapeAttribute('"<&>')).toBe('&quot;&lt;&amp;&gt;');
  });
});

describe('self-hosted approved scene assets',()=>{
  const root=resolve(import.meta.dirname,'..');
  it.each([SCENE_ASSET,STYLE_ASSET])('ships content-addressed asset %s',asset=>{
    const path=resolve(root,'public'+asset);
    expect(existsSync(path)).toBe(true);
    const hash=createHash('sha256').update(readFileSync(path)).digest('hex').slice(0,16);
    expect(asset).toContain(hash);
    expect(asset).toMatch(/^\/assets\/cinematic\/seasons-[a-f0-9]{16}\.(js|css)$/);
  });
  it('uses same-origin destinations without a preview-server dependency',()=>{
    const html=readFileSync(resolve(root,'src/features/cinematic/template.html'),'utf8');
    expect(html).toContain('id="home" aria-label="메인 플랫폼으로 바로 이동"');
    expect(html).toContain('href="__PLATFORM_DESTINATION__" target="_top"');
    expect(html).not.toMatch(/localhost:5196|192\.168\.|node_modules\/|type="importmap"/);
    expect(html).toContain('__CINEMATIC_SCRIPT__');
    expect(html).toContain('__CINEMATIC_STYLE__');
  });
  it('keeps the MIT license and releases runtime resources on departure',()=>{
    const scene=readFileSync(resolve(root,'cinematic/scene.js'),'utf8');
    expect(scene).toContain('cancelAnimationFrame(animationId)');
    expect(scene).toContain('audioContext.close()');
    expect(scene).toContain('renderer?.dispose()');
    expect(scene).toContain("$('home').addEventListener('click',enterPlatform)");
    const bundle=readFileSync(resolve(root,'public'+SCENE_ASSET),'utf8');
    expect(bundle).toContain('Permission is hereby granted, free of charge');
    expect(bundle).not.toMatch(/\/Users\/|localhost:5196|192\.168\.0\.16/);
  });
});


describe('platform cinematic navigation',()=>{
  const root=resolve(import.meta.dirname,'..');
  it('opens the cinematic page with the exact approved button label',()=>{
    const source=readFileSync(resolve(root,'src/features/cinematic/CinematicNavLink.tsx'),'utf8');
    expect(source).toContain('href="/platform/intro"');
    expect(source).toContain('시네마틱 페이지로 이동');
    const platform=readFileSync(resolve(root,'src/features/platform/Platform.tsx'),'utf8');
    expect(platform).toContain('<CinematicNavLink/><a href="/platform#services">서비스 소개</a>');
  });
  it('requires an explicit home click instead of navigating at the end of playback',()=>{
    const source=readFileSync(resolve(root,'cinematic/scene.js'),'utf8');
    expect(source.match(/notifyParent\('mlo:cinematic:enter'\)/g)?.length).toBe(1);
    expect(source).toContain("$('enter').addEventListener('click',enterPlatform)");
    expect(source).toContain("$('home').addEventListener('click',enterPlatform)");
    const finish=source.slice(source.indexOf('function finish()'),source.indexOf("$('skip').addEventListener"));
    expect(finish).not.toContain('enterPlatform');
    expect(finish).not.toContain('location.assign');
    expect(finish).not.toContain('mlo:cinematic:enter');
  });
});


describe('animation clock boundaries',()=>{
  it('never sends the initial timeline below zero when RAF predates initialization',()=>{
    expect(safeFrameDelta(1000,1008)).toBe(0);
    expect(Math.floor(safeFrameDelta(1000,1008)/7)).toBe(0);
  });
  it('caps long frames and handles invalid clock values',()=>{
    expect(safeFrameDelta(1016,1000)).toBe(0.016);
    expect(safeFrameDelta(1500,1000)).toBe(0.12);
    expect(safeFrameDelta(NaN,1000)).toBe(0);
    expect(safeFrameDelta(1000,Infinity)).toBe(0);
  });
});
