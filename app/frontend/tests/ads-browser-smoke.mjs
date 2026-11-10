// Synthetic browser regression: real React UI, intercepted API; no production writes.
import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {mkdtemp, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createServer} from 'vite';
import {chromium} from 'playwright';

const root=fileURLToPath(new URL('../',import.meta.url));
const executable=[process.env.ADS_TEST_BROWSER_PATH,
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe'].find(value=>value&&existsSync(value));
if(!executable)throw new Error('Set ADS_TEST_BROWSER_PATH to an installed Chromium browser. No browser is downloaded.');
const directory=await mkdtemp(path.join(os.tmpdir(),'mlo-ads-browser-'));
let server,browser;
const checks=[];
try{
  server=await createServer({root,configLoader:'runner',cacheDir:path.join(directory,'vite-cache'),
    optimizeDeps:{entries:[path.join(root,'index.html')],include:['react','react-dom/client','react/jsx-runtime']},
    server:{host:'127.0.0.1',port:0,strictPort:false}});
  await server.listen();
  const base=server.resolvedUrls.local[0].replace(/\/$/,'');
  browser=await chromium.launch({executablePath:executable,headless:true});
  const context=await browser.newContext({locale:'ko-KR',timezoneId:'Asia/Seoul',serviceWorkers:'block'});
  const csrf='s'.repeat(32);
  await context.addCookies([{name:'platform_csrf',value:csrf,url:base,sameSite:'Lax'}]);
  const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==','base64');
  const counters={impressions:0,matched_clicks:0,early_clicks:0,unmatched_clicks:0,pending_clicks:0,ctr_percent:null};
  const seed={ad_id:'seed',title:'초 단위 원본 광고',asset_id:'synthetic-asset',link_url:'https://example.com/ad',
    placement:'top',starts_at:'2026-11-09T01:00:59.123Z',ends_at:'2026-11-09T05:00:45.678Z',
    priority:1,daily_budget_tokens:100,total_budget_tokens:1000,version:1,paused:false,status:'ACTIVE',
    measurement:{status:'READY',impressions:0,clicks:0}};
  const rows=[{...seed}],requests=[],replays=new Map();
  let analytics='pending',role='ADMIN',uploads=0,creates=0;
  const json=(route,body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
  await context.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url());
    if(url.origin!==base)return route.abort();
    if(!url.pathname.startsWith('/api/'))return route.continue();
    const method=req.method(),pathname=url.pathname;
    if(method!=='GET'){
      requests.push({pathname,method,headers:req.headers(),body:req.postDataJSON?.bind(req)});
      if(req.headers()['x-csrftoken']!==csrf)return json(route,{code:'FORBIDDEN',message:'synthetic CSRF rejection'},403);
    }
    if(pathname==='/api/auth/me')return json(route,{authenticated:true,role,
      allowed_menus:role==='ADMIN'?[{path:'/admin/ads',label:'광고 관리'}]:[],csrf_token:csrf});
    if(method==='GET'&&pathname.startsWith('/api/admin/ad-assets/'))
      return route.fulfill({status:200,contentType:'image/png',body:png});
    if(method==='POST'&&pathname==='/api/admin/ad-assets'){
      uploads++;assert.match(req.headers()['content-type'],/multipart\/form-data/);
      assert.ok(req.postDataBuffer().includes(Buffer.from('name="image"')));
      return json(route,{asset_id:'synthetic-asset',mime_type:'image/png',byte_size:png.length,width:1,height:1},201);
    }
    if(pathname==='/api/admin/ads/summary')return json(route,{
      counts:{ACTIVE:rows.filter(x=>!x.paused).length,RESERVED:0,ENDED:0,STOPPED:rows.filter(x=>x.paused).length},
      evaluated_at:'2026-11-09T02:00:00Z',measurement:{status:'READY',impressions:0,clicks:0}});
    if(pathname==='/api/admin/ads/analytics'){
      if(analytics==='html')return route.fulfill({status:200,contentType:'text/html',body:'<html>proxy response</html>'});
      if(analytics==='null')return json(route,null);
      const available=analytics==='ready';
      return json(route,{enabled:true,report_available:available,status:available?'PROVISIONAL':'PENDING',
        policy_confirmed:false,is_stale:!available,scope:{},rows:[],
        totals:available?counters:Object.fromEntries(Object.keys(counters).map(name=>[name,null])),
        report_id:available?'synthetic-report':null,as_of:available?'2026-11-09T02:00:00Z':null,
        source_snapshot:available?{report_id:'synthetic-report',as_of:'2026-11-09T02:00:00Z',scope:{}}:null});
    }
    if(method==='GET'&&pathname==='/api/admin/ads'){
      const selected=rows.filter(row=>(!url.searchParams.get('placement')||row.placement===url.searchParams.get('placement'))&&
        (!url.searchParams.get('status')||row.status===url.searchParams.get('status')));
      return json(route,{items:selected,next_cursor:null,as_of:'2026-11-09T02:00:00Z'});
    }
    const body=req.postDataJSON();
    if(pathname==='/api/admin/ads/preview')return json(route,{asset_id:body.asset_id,title:body.title,placement:body.placement,operational_delivery:false});
    const token=req.headers()['idempotency-key'];assert.ok(token);
    const signature=method+pathname+token;
    if(replays.has(signature))return json(route,replays.get(signature).body,replays.get(signature).status);
    let result,status=200;
    if(method==='POST'&&pathname==='/api/admin/ads'){
      creates++;status=201;result={...body,ad_id:'new-'+creates,version:1,paused:false,status:'ACTIVE',
        measurement:{status:'READY',impressions:0,clicks:0}};rows.push(result);
    }else{
      const row=rows.find(item=>pathname==='/api/admin/ads/'+item.ad_id||pathname==='/api/admin/ads/'+item.ad_id+'/stop');
      assert.ok(row,'Unexpected API path '+pathname);
      if(row.version!==body.version)return json(route,{code:'VERSION_CONFLICT',message:'synthetic version conflict'},409);
      if(pathname.endsWith('/stop'))Object.assign(row,{paused:true,status:'STOPPED',version:row.version+1});
      else Object.assign(row,body,{version:row.version+1});
      result={...row};
    }
    replays.set(signature,{body:result,status});
    return json(route,result,status);
  });
  const page=await context.newPage();
  await page.goto(base+'/admin/ads');
  await page.getByRole('button',{name:'＋ 새 광고 등록',exact:true}).waitFor();
  await page.getByRole('row').filter({hasText:seed.title}).waitFor();
  const fill=async()=>{
    await page.getByLabel('제목',{exact:true}).fill('동일 내용 신규 광고');
    await page.getByLabel('이동 링크',{exact:true}).fill('https://example.com/new');
    await page.getByLabel('게시 시작 · KST',{exact:true}).fill('2026-11-09T10:00');
    await page.getByLabel('게시 종료 · KST',{exact:true}).fill('2026-11-09T14:00');
    const details=page.locator('details.ads-budget');
    if((await details.getAttribute('open'))===null)await details.locator('summary').click();
    await page.getByLabel('일일 한도',{exact:true}).fill('100');
    await page.getByLabel('전체 한도',{exact:true}).fill('1000');
    await page.getByLabel('배너 이미지',{exact:true}).setInputFiles({name:'synthetic.png',mimeType:'image/png',buffer:png});
  };
  const responseFor=(pathname,method)=>page.waitForResponse(res=>new URL(res.url()).pathname===pathname&&res.request().method()===method);
  await fill();
  await Promise.all([responseFor('/api/admin/ads/preview','POST'),page.getByRole('button',{name:'편집 내용 미리보기',exact:true}).click()]);
  await page.getByRole('status').filter({hasText:'미리보기를 갱신했습니다.'}).waitFor();
  assert.equal(creates,0);checks.push('preview creates no operational ad');
  await Promise.all([responseFor('/api/admin/ads','POST'),page.getByRole('button',{name:'저장',exact:true}).click()]);
  await page.getByRole('status').filter({hasText:'광고를 등록했습니다.'}).waitFor();
  assert.equal(creates,1);assert.equal(uploads,1);checks.push('platform-cookie asset upload and registration');
  await page.getByRole('button',{name:'＋ 새 광고 등록',exact:true}).click();
  await fill();
  await page.getByLabel('일일 한도',{exact:true}).fill('9007199254740993');
  // Native number validity may stop form submit first; preview also exercises
  // the same custom validation without relying on browser validity UI.
  await page.getByRole('button',{name:'편집 내용 미리보기',exact:true}).click();
  await page.getByRole('alert').waitFor();
  assert.equal(creates,1);assert.equal(uploads,1);checks.push('unsafe budget rejected before write');
  await page.getByLabel('일일 한도',{exact:true}).fill('100');
  await Promise.all([responseFor('/api/admin/ads','POST'),page.getByRole('button',{name:'저장',exact:true}).click()]);
  await page.getByRole('status').filter({hasText:'광고를 등록했습니다.'}).waitFor();
  assert.equal(creates,2);
  const createRequests=requests.filter(x=>x.pathname==='/api/admin/ads'&&x.method==='POST');
  assert.equal(createRequests.length,2);
  assert.notEqual(createRequests[0].headers['idempotency-key'],createRequests[1].headers['idempotency-key']);
  checks.push('identical new registrations use distinct intentions');
  await page.getByRole('row').filter({hasText:seed.title}).getByRole('button',{name:'✎ 수정',exact:true}).click();
  await page.getByLabel('제목',{exact:true}).fill('초 단위 보존 수정');
  await Promise.all([responseFor('/api/admin/ads/seed','PUT'),page.getByRole('button',{name:'저장',exact:true}).click()]);
  await page.getByRole('status').filter({hasText:'광고를 수정했습니다.'}).waitFor();
  assert.equal(rows[0].starts_at,seed.starts_at);assert.equal(rows[0].ends_at,seed.ends_at);
  checks.push('title edit preserves exact start and end timestamps');
  await Promise.all([responseFor('/api/admin/ads/seed/stop','POST'),
    page.getByRole('row').filter({hasText:'초 단위 보존 수정'}).getByRole('button',{name:'Ⅱ 게시 중단',exact:true}).click()]);
  await page.getByRole('status').filter({hasText:'광고 게시를 중단했습니다.'}).waitFor();
  assert.equal(rows[0].paused,true);checks.push('manual stop uses current version and platform CSRF');
  await page.getByRole('button',{name:'성과 리포트',exact:true}).click();
  await page.getByRole('status').filter({hasText:'성과 집계를 기다리고 있습니다.'}).waitFor();
  checks.push('pending report differs from zero');
  analytics='html';
  await page.getByRole('button',{name:'성과 조회',exact:true}).click();
  await page.getByRole('alert').waitFor();checks.push('malformed HTTP 200 produces visible retryable error');
  analytics='null';
  await page.getByRole('button',{name:'다시 조회',exact:true}).click();
  await page.getByRole('alert').waitFor();
  await page.getByRole('button',{name:'다시 조회',exact:true}).waitFor({state:'visible'});
  checks.push('JSON null report produces visible error');
  analytics='ready';
  await page.getByRole('button',{name:'다시 조회',exact:true}).click();
  await page.getByRole('status').filter({hasText:'선택한 조건의 집계 결과는 0건입니다.'}).waitFor();
  checks.push('report retry recovers to genuine zero');
  role='USER';
  const other=await context.newPage();await other.goto(base+'/admin/ads');
  await other.getByText('광고 관리 권한을 확인해 주세요.',{exact:false}).waitFor();
  assert.equal(await other.getByRole('button',{name:'＋ 새 광고 등록',exact:true}).count(),0);
  checks.push('non-admin cannot see registration controls');
  await context.close();
  console.log(JSON.stringify({mode:'REAL_BROWSER_SYNTHETIC_API',browser:path.basename(executable),
    production_services_contacted:false,passed:checks.length,checks},null,2));
}finally{
  await browser?.close();await server?.close();
  const resolved=path.resolve(directory),temporaryRoot=path.resolve(os.tmpdir())+path.sep;
  if(!resolved.startsWith(temporaryRoot)||!path.basename(resolved).startsWith('mlo-ads-browser-'))
    throw new Error('Refusing cleanup outside the generated test directory.');
  await rm(resolved,{recursive:true,force:true,maxRetries:5,retryDelay:200});
}
