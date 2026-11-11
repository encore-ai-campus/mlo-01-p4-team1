import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {preview} from 'vite';

const service=await preview({configFile:false,preview:{host:'127.0.0.1',port:0}});
const origin=`http://127.0.0.1:${service.httpServer.address().port}`;
const executablePath=process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
let browser;
try{
  browser=await chromium.launch({headless:true,...(executablePath?{executablePath}:{})});
  const page=await browser.newPage();
  const csrf='mock-admin-csrf';
  const claim='53abdd69-f6b1-4584-a1bb-03a185c15221';
  const reviewApi='/api/platform/v1/admin/workspace-claims';
  const claimApi=`${reviewApi}/${claim}`;
  const claimPage=`/platform/review?claim=${claim}`;
  const memberRequests=[];
  let adminState='anonymous';
  let reviewerAllowed=true;
  let claimState='SUBMITTED';
  let reviewRequests=0;
  let loginHeader;
  let loginLanding='/admin/passkey';
  let authAvailable=true;

  await page.route('**/*',async route=>{
    const request=route.request();
    const url=new URL(request.url());
    if(url.origin!==origin)return route.abort();
    if(!url.pathname.startsWith('/api/'))return route.continue();
    if(url.pathname==='/api/auth/me'){
      if(!authAvailable)return route.fulfill({status:503,json:{message:'관리자 인증 서비스 확인 필요'}});
      const canReview=adminState==='verified'&&reviewerAllowed;
      return route.fulfill({json:{
        csrf_token:csrf,authenticated:adminState==='verified',
        role:adminState==='verified'?'ADMIN':null,passkey_required:adminState==='pending',
        platform_enabled:false,private_demo_review_allowed:canReview,
        allowed_menus:adminState==='verified'?[{path:'/admin/music',label:'음악 관리'},
          ...(canReview?[{path:'/platform/review',label:'매장 권한 심사'}]:[])]:[],
      }});
    }
    if(url.pathname==='/api/auth/login'){
      loginHeader=request.headers()['x-csrftoken'];
      adminState='pending';
      return route.fulfill({json:{passkey_required:true,role:null,landing_path:loginLanding}});
    }
    if(url.pathname==='/api/accounts/policy'){
      return route.fulfill({json:{version:'',ready:false,local_preview:false,documents:{}}});
    }
    if(url.pathname===claimApi||url.pathname===reviewApi){
      assert.equal(adminState,'verified');
      assert.equal(reviewerAllowed,true);
      reviewRequests+=1;
      const fixture={
        id:claim,state:claimState,display_name:'합성 카페',address:'합성 주소',
        revision:2,content_revision:1,match_result:'MATCHED',business_status:'ACTIVE',
        review_context:{fixture_id:'demo-active-001',applicant_label:'시연점주'},
      };
      return route.fulfill({json:{data:url.pathname===reviewApi?{items:[fixture],next_cursor:null}:fixture}});
    }
    if(url.pathname.startsWith('/api/platform/v1/'))memberRequests.push(url.pathname);
    return route.fulfill({status:503,json:{error:{code:'PLATFORM_NOT_READY',message:'공개 회원 기능은 닫혀 있습니다.'}}});
  });

  const submit=async()=>{
    await page.getByLabel('로그인 아이디').fill('fixture-admin');
    await page.getByLabel('로그인 비밀번호').fill('fixture-password');
    await page.getByRole('button',{name:'로그인',exact:true}).click();
  };

  await page.goto(origin+'/platform/admin/login');
  await submit();
  await page.waitForURL(origin+'/admin/passkey');
  await page.getByRole('heading',{name:'관리자 패스키 인증'}).waitFor();
  assert.equal(loginHeader,csrf);

  await page.goto(origin+'/platform/admin');
  await page.getByRole('link',{name:'패스키 인증'}).waitFor();
  assert.equal(await page.getByRole('link',{name:'음악 관리'}).count(),0);

  adminState='verified';
  await page.goto(origin+'/platform/admin');
  await page.getByRole('link',{name:'음악 관리'}).waitFor();
  await page.getByRole('link',{name:/매장 권한 심사/}).click();
  await page.waitForURL(origin+'/platform/review');
  await page.getByRole('heading',{name:'합성 카페'}).waitFor();
  await page.getByRole('button',{name:'승인',exact:true}).waitFor();

  claimState='APPROVED';
  await page.getByRole('button',{name:'새로고침'}).click();
  await page.getByText('승인 완료',{exact:true}).waitFor();
  assert.equal(await page.getByRole('button',{name:'승인',exact:true}).count(),0);

  await page.goto(origin+claimPage);
  await page.getByText('승인 완료',{exact:true}).waitFor();

  reviewerAllowed=false;
  const authorizedReviewRequests=reviewRequests;
  await page.goto(origin+'/platform/admin');
  await page.getByRole('link',{name:'음악 관리'}).waitFor();
  assert.equal(await page.getByRole('link',{name:/매장 권한 심사/}).count(),0);
  await page.goto(origin+'/platform/review');
  await page.getByText('매장 심사 권한이 없습니다.').waitFor();
  assert.equal(reviewRequests,authorizedReviewRequests);

  adminState='anonymous';
  loginLanding='https://invalid.example/passkey';
  await page.goto(origin+'/platform/admin/login');
  await submit();
  await page.getByRole('alert').getByText('관리자 추가 인증 경로를 확인해 주세요.').waitFor();
  assert.equal(page.url(),origin+'/platform/admin/login');

  authAvailable=false;
  await page.goto(origin+'/platform/admin/login');
  await page.getByRole('alert').getByText('관리자 인증 서비스 확인 필요').waitFor();
  assert.equal(await page.getByLabel('로그인 아이디').count(),0);
  assert.deepEqual(memberRequests,[]);
  console.log('Admin login CSRF, passkey handoff, scoped menu, pending/approved claim, and denied access verified with member routes closed.');
}finally{
  if(browser)await browser.close();
  await new Promise((resolve,reject)=>service.httpServer.close(error=>error?reject(error):resolve()));
}
