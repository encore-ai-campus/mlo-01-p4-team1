/* Real LAN HTTP UI acceptance. No passkey ceremonies or virtual authenticators. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {randomUUID,randomBytes}=require('node:crypto');
const input=JSON.parse(fs.readFileSync('/mvp-private/local-inputs.json','utf8'));
const origin=process.env.PLATFORM_TEST_ORIGIN;
if(!origin)throw new Error('Set PLATFORM_TEST_ORIGIN to the configured synthetic LAN origin.');
const evidence='/evidence/'+(process.env.PLATFORM_EVIDENCE_SUBDIR||'ui-review');
const checks=[],errors=[],requests=[];
const signupPassword='Ui'+randomBytes(5).toString('hex')+'!@';
async function stage(name,work){console.log('RUN '+name);await work();checks.push(name);}
async function pageFor(browser){
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  await context.addInitScript(()=>sessionStorage.setItem('mlo:seasonal-intro:seen:v1','1'));
  const page=await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));page.on('request',request=>requests.push(request.url()));
  return {context,page};
}
async function api(page,path,method='GET',body){
  return page.evaluate(async({path,method,body,id})=>{
    const base='/api/platform/v1';const boot=await(await fetch(base+'/sessions/current')).json();
    const result=await fetch(base+path,{method,headers:{'Content-Type':'application/json','X-CSRFToken':boot.data.session?.csrf_token||boot.data.csrf_token,'Idempotency-Key':id},...(body===undefined?{}:{body:JSON.stringify(body)})});
    return {status:result.status,body:result.status===204?null:await result.json()};
  },{path,method,body,id:randomUUID()});
}
async function login(page,name,expected,password=input.password){
  await page.goto(origin+'/platform/login');
  await page.getByLabel('로그인 아이디',{exact:true}).fill(name);
  await page.getByLabel('로그인 비밀번호',{exact:true}).fill(password);
  await page.getByRole('button',{name:'로그인',exact:true}).click();await page.waitForURL(origin+expected);
}
async function agreements(page){for(const text of ['[필수] 서비스 이용약관에 동의합니다.','[필수 확인] 기본 회원정보 처리 안내를 확인했습니다.','[필수 확인] 만 14세 이상입니다.'])await page.getByLabel(text,{exact:true}).check();}
async function secret(page){await page.getByLabel('가입 비밀번호',{exact:true}).fill(signupPassword);await page.getByLabel('가입 비밀번호 확인',{exact:true}).fill(signupPassword);}
async function waitText(page,selector,text){await page.locator(selector).filter({hasText:text}).waitFor();}
async function mailbox(address,subject){
  for(let i=0;i<100;i++){
    const list=await(await fetch('http://mailpit:8025/api/v1/messages')).json();
    const message=list.messages.find(item=>item.Subject===subject&&item.To.some(to=>to.Address===address));
    if(message)return (await(await fetch('http://mailpit:8025/api/v1/message/'+message.ID)).json()).Text;
    await new Promise(resolve=>setTimeout(resolve,250));
  }
  throw new Error('Synthetic Mailpit message did not arrive');
}

(async()=>{
  fs.mkdirSync(evidence,{recursive:true});const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try{
    const guest=await pageFor(browser);const page=guest.page;
    await stage('anonymous_header_service_banners_and_responsive_layout',async()=>{
      await page.goto(origin+'/platform');await page.locator('.p-hero').waitFor();
      assert.equal(await page.locator('a[href="/platform/account"],a[href="/platform/review"],.p-sidebar').count(),0);
      assert.equal(await page.locator('.p-header-account a[href="/platform/signup"]').count(),1);
      assert.equal(await page.locator('.p-header-account a[href="/platform/login"]').count(),1);
      await page.screenshot({path:evidence+'/01-public-home.png',fullPage:true});
      for(const width of [390,320]){
        await page.setViewportSize({width,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
        if(width===390)await page.screenshot({path:evidence+'/02-public-mobile.png',fullPage:true});
      }
      await page.setViewportSize({width:1440,height:1000});
      const secure=await page.evaluate(()=>({secure:isSecureContext,randomUUID:typeof crypto.randomUUID}));
      assert.equal(secure.secure,false);assert.equal(secure.randomUUID,'undefined');
    });
    let recoveryPage,recoveryUser,recoveryAddress;
    const consumerName='ui'+Date.now().toString(36)+'member';
    await stage('signup_duplicate_checks_invalidation_and_password_feedback',async()=>{
      await page.goto(origin+'/platform/signup');await page.getByLabel('가입 복구 이메일',{exact:true}).waitFor();
      assert.equal(await page.getByLabel('가입 복구 이메일',{exact:true}).isEnabled(),true);
      assert(await page.evaluate(()=>Boolean(document.querySelector('#platform-nickname').compareDocumentPosition(document.querySelector('#platform-email'))&Node.DOCUMENT_POSITION_FOLLOWING)));
      await page.getByLabel('가입 아이디',{exact:true}).fill('MVPOWNER');await page.getByRole('button',{name:'아이디 중복 확인',exact:true}).click();await waitText(page,'#username-check','이미 사용 중인');
      await page.getByLabel('가입 아이디',{exact:true}).fill(consumerName);assert.equal(await page.locator('#username-check').textContent(),'');
      await page.getByRole('button',{name:'아이디 중복 확인',exact:true}).click();await waitText(page,'#username-check','사용 가능한');
      await page.getByLabel('가입 별명',{exact:true}).fill('공 백');await page.getByRole('button',{name:'별명 중복 확인',exact:true}).click();await waitText(page,'#nickname-check','공백 불가');
      await page.getByLabel('가입 별명',{exact:true}).fill('한글Ab12');await page.getByRole('button',{name:'별명 중복 확인',exact:true}).click();await waitText(page,'#nickname-check','사용 가능한');
      assert.equal(await page.getByLabel('가입 별명',{exact:true}).getAttribute('maxlength'),'10');
      await page.getByLabel('가입 별명',{exact:true}).fill('');
      await page.getByLabel('가입 비밀번호',{exact:true}).fill('short');await waitText(page,'#password-length','현재 5자');
      await page.getByLabel('가입 비밀번호',{exact:true}).fill(signupPassword);
      await page.getByLabel('가입 비밀번호 확인',{exact:true}).fill('different-synthetic-password');await waitText(page,'#password-match','일치하지 않습니다');
      const before=requests.filter(url=>url.endsWith('/signups')).length;
      await page.getByRole('button',{name:'회원가입하고 시작하기'}).click();await page.getByRole('alert').filter({hasText:'비밀번호 확인이 일치하지 않습니다'}).waitFor();
      assert.equal(requests.filter(url=>url.endsWith('/signups')).length,before);
      await page.getByLabel('가입 비밀번호',{exact:true}).fill('lowercase!!');await waitText(page,'#password-length','대문자 1개 이상 필요');
      await page.getByLabel('가입 비밀번호',{exact:true}).fill('Uppercase!');await waitText(page,'#password-length','특수문자 2개 이상 필요');
      await page.getByLabel('가입 비밀번호',{exact:true}).fill('Upper case!!');await waitText(page,'#password-length','공백 없이');
      await secret(page);await waitText(page,'#password-match','비밀번호가 일치합니다');
      assert.equal(await page.getByRole('alert').count(),0);
      await page.screenshot({path:evidence+'/03-signup-validation.png',fullPage:true});
      for(const width of [390,320]){await page.setViewportSize({width,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));}
      await page.setViewportSize({width:1440,height:1000});
    });
    await stage('server_rejects_weak_password_even_with_matching_confirmation',async()=>{
      await agreements(page);await page.getByLabel('가입 비밀번호',{exact:true}).fill(consumerName.toUpperCase()+'!@');await page.getByLabel('가입 비밀번호 확인',{exact:true}).fill(consumerName.toUpperCase()+'!@');
      await page.getByRole('button',{name:'회원가입하고 시작하기'}).click();await page.getByRole('alert').filter({hasText:'흔하거나 계정 정보와 비슷한'}).waitFor();
      assert.equal((await api(page,'/availability','POST',{username:consumerName})).body.data.available,true);
      await secret(page);
    });
    await stage('unconsented_optional_email_never_enters_signup_payload_or_account',async()=>{
      await page.getByLabel('가입 복구 이메일',{exact:true}).fill('unconsented-synthetic@example.invalid');
      assert.equal(await page.getByLabel('[선택] 복구 이메일 수집·이용에 동의합니다.',{exact:true}).isChecked(),false);
      const payload=page.waitForRequest(request=>request.url().endsWith('/signups')&&request.method()==='POST');
      await page.getByRole('button',{name:'회원가입하고 시작하기'}).click();
      const body=(await payload).postDataJSON();assert.equal(body.email_consent,false);assert.equal(Object.hasOwn(body,'email'),false);
      await page.waitForURL(origin+'/platform/consumer');
      const profile=(await api(page,'/me')).body.data;assert.equal(profile.email_status,'ABSENT');assert.match(profile.nickname,/^회원\d{4}$/);
      assert.equal(await page.locator('a[href="/platform/review"]').count(),0);
      await page.goto(origin+'/platform/business');await page.getByRole('heading',{name:'이용 유형을 먼저 추가해 주세요'}).waitFor();
    });
    await stage('single_membership_login_routes_from_database_without_tabs',async()=>{
      const single=await pageFor(browser);await single.page.goto(origin+'/platform/login');await single.page.getByLabel('로그인 아이디',{exact:true}).waitFor();
      assert.equal(await single.page.locator('.p-tabs,[role="tab"]').count(),0);
      assert.equal(await single.page.getByRole('button',{name:'심사 관리자',exact:true}).count(),0);
      await login(single.page,'mvpconsumer','/platform/consumer');
      const business=await pageFor(browser);await login(business.page,'mvpowner','/platform/business');
      assert.equal(await business.page.locator('a[href="/platform/review"]').count(),0);
      await business.page.screenshot({path:evidence+'/04-member-business.png',fullPage:true});
    });
    await stage('dual_membership_selects_after_login_and_updates_only_this_session',async()=>{
      await page.goto(origin+'/platform/account');await page.getByLabel('약관에 동의하고 처리 안내를 확인했습니다.').check();
      await page.getByRole('button',{name:'사업자 유형 추가',exact:true}).click();await page.waitForURL(origin+'/platform/business');
      assert.equal((await api(page,'/sessions/current')).body.data.session.active_membership,'BUSINESS');
      const another=await pageFor(browser);await login(another.page,consumerName,'/platform',signupPassword);await another.page.getByRole('heading',{name:'이용할 서비스를 선택하세요'}).waitFor();
      await another.page.getByRole('button',{name:'사용자 서비스로 이동',exact:true}).click();await another.page.waitForURL(origin+'/platform/consumer');
      assert.equal((await api(another.page,'/sessions/current')).body.data.session.active_membership,'CONSUMER');
      assert.equal((await api(page,'/sessions/current')).body.data.session.active_membership,'BUSINESS');
    });
    await stage('consented_email_is_optional_and_starts_pending_verification',async()=>{
      const emailPage=(await pageFor(browser)).page;recoveryPage=emailPage;recoveryUser='uie'+Date.now().toString(36);recoveryAddress='synthetic-'+Date.now().toString(36)+'@example.invalid';await emailPage.goto(origin+'/platform/signup');
      await emailPage.getByLabel('가입 아이디',{exact:true}).fill(recoveryUser);await secret(emailPage);await agreements(emailPage);
      await emailPage.getByLabel('가입 복구 이메일',{exact:true}).fill(recoveryAddress);
      await emailPage.getByLabel('[선택] 복구 이메일 수집·이용에 동의합니다.',{exact:true}).check();
      await emailPage.getByRole('button',{name:'회원가입하고 시작하기'}).click();await emailPage.waitForURL(origin+'/platform/consumer');
      assert.equal((await api(emailPage,'/me')).body.data.email_status,'PENDING');
      await emailPage.goto(origin+'/platform/account');await emailPage.getByRole('link',{name:'확인 코드 입력하기',exact:true}).waitFor();
    });
    await stage('anonymous_direct_review_and_account_routes_do_not_load_private_panels',async()=>{
      const isolated=(await pageFor(browser)).page;
      await isolated.goto(origin+'/platform/review');await isolated.getByRole('heading',{name:'접근 권한이 없습니다'}).waitFor();
      assert.equal(await isolated.getByRole('heading',{name:'승인 대기 신청'}).count(),0);
      await isolated.goto(origin+'/platform/account');await isolated.getByRole('heading',{name:'계정으로 시작하세요'}).waitFor();
    });
    await stage('public_policy_footer_links_are_accessible_without_login',async()=>{
      const publicPage=(await pageFor(browser)).page;
      for(const target of ['/platform','/platform/login','/platform/signup','/platform/account/recovery']){
        await publicPage.goto(origin+target);await publicPage.locator('.p-footer').waitFor();
        for(const [name,path] of [['이용약관','/platform/terms'],['개인정보 처리방침','/platform/privacy'],['개인정보 관련 문의','/platform/privacy-contact']])assert.equal(await publicPage.locator('.p-footer').getByRole('link',{name,exact:true}).getAttribute('href'),path);
      }
      await publicPage.goto(origin+'/platform/terms');await publicPage.getByRole('heading',{name:'서비스 이용약관',exact:true}).waitFor();
      await publicPage.goto(origin+'/platform/privacy');await publicPage.getByRole('heading',{name:'개인정보 처리방침',exact:true}).waitFor();
      await publicPage.goto(origin+'/platform/privacy-contact');await publicPage.getByRole('status').filter({hasText:'개인정보 문의처 미확정'}).waitFor();
      await publicPage.screenshot({path:evidence+'/05-privacy-contact.png',fullPage:true});
    });
    await stage('business_popup_and_guided_verification_to_review_submission',async()=>{
      const businessPage=(await pageFor(browser)).page;
      await businessPage.goto(origin+'/platform/signup?type=BUSINESS');
      const dialog=businessPage.getByRole('dialog',{name:'사업자 가입은 이렇게 진행됩니다'});await dialog.waitFor();
      assert.equal(await dialog.locator('li').count(),4);assert(await dialog.innerText().then(text=>text.includes('실제 국세청 API 조회')));
      await businessPage.screenshot({path:evidence+'/06-business-popup.png',fullPage:true});
      await businessPage.setViewportSize({width:390,height:844});assert(await businessPage.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      await businessPage.screenshot({path:evidence+'/07-business-popup-mobile.png',fullPage:true});
      await businessPage.keyboard.press('Escape');await dialog.waitFor({state:'hidden'});
      await businessPage.getByRole('button',{name:'사업자 가입 절차 다시 보기'}).click();await dialog.waitFor();await dialog.getByRole('button',{name:'확인하고 가입 계속하기'}).click();
      await businessPage.setViewportSize({width:1440,height:1000});
      await businessPage.getByLabel('가입 아이디',{exact:true}).fill('jb'+Date.now().toString(36));await secret(businessPage);await agreements(businessPage);
      await businessPage.getByRole('button',{name:'회원가입하고 시작하기'}).click();await businessPage.waitForURL(origin+'/platform/business');
      await businessPage.getByRole('heading',{name:'사업자 계정에서 매장 운영까지'}).waitFor();
      await businessPage.getByLabel('합성 사업자 시나리오',{exact:true}).selectOption('demo-mismatch');
      await businessPage.getByRole('button',{name:'사업자 정보 확인하기',exact:true}).click();
      await businessPage.locator('.p-claim-progress').filter({hasText:'사업자 정보가 일치하지 않습니다'}).waitFor();
      assert.equal(await businessPage.getByRole('button',{name:'매장 관리권한 심사 요청',exact:true}).isDisabled(),true);
      await businessPage.getByRole('button',{name:'정보 수정·다시 확인',exact:true}).click();
      await businessPage.getByLabel('합성 사업자 시나리오',{exact:true}).selectOption('demo-branch-002');await businessPage.getByRole('button',{name:'사업자 정보 확인하기',exact:true}).click();
      await businessPage.locator('.p-claim-progress').filter({hasText:'사업자 정보 확인이 완료되었습니다'}).waitFor();
      await businessPage.getByRole('button',{name:'매장 관리권한 심사 요청',exact:true}).click();await businessPage.locator('.p-claim-progress').filter({hasText:'관리권한 심사를 기다리고 있습니다'}).waitFor();
      await businessPage.screenshot({path:evidence+'/08-business-review-pending.png',fullPage:true});
      const approved=(await pageFor(browser)).page;await login(approved,'mvpowner','/platform/business');await approved.getByRole('button',{name:'승인 매장 열기',exact:true}).first().click();await approved.getByRole('heading',{name:/음악 운영$/}).waitFor();
    });
    await stage('legacy_account_urls_redirect_to_platform_including_recovery_fragment',async()=>{
      const alias=(await pageFor(browser)).page;
      for(const [old,current] of [['/login','/platform/login'],['/signup','/platform/signup'],['/account/recovery','/platform/account/recovery'],['/mypage','/platform/account']]){
        await alias.goto(origin+old);await alias.waitForURL(origin+current);
      }
    });
    await stage('platform_admin_primary_login_stops_at_deferred_step_up',async()=>{
      const administrator=(await pageFor(browser)).page;await administrator.goto(origin+'/platform/login');await administrator.getByRole('link',{name:'플랫폼 관리자 로그인',exact:true}).click();
      await administrator.getByLabel('로그인 아이디',{exact:true}).fill('mvpreviewer');await administrator.getByLabel('로그인 비밀번호',{exact:true}).fill(input.password);await administrator.getByRole('button',{name:'로그인',exact:true}).click();
      await administrator.waitForURL(origin+'/platform/admin');await administrator.getByRole('heading',{name:'관리자 1차 로그인을 완료했습니다'}).waitFor();
      const me=await administrator.evaluate(async()=>await(await fetch('/api/auth/me')).json());assert.equal(me.authenticated,false);assert.equal(me.passkey_required,true);assert.deepEqual(me.allowed_menus,[]);
      assert.equal((await api(administrator,'/admin/workspace-claims')).status,403);
      await administrator.screenshot({path:evidence+'/09-platform-admin-pending.png',fullPage:true});
    });
    await stage('central_account_email_verification_and_id_recovery_mail',async()=>{
      const emailText=await mailbox(recoveryAddress,'복구 이메일 확인');const code=emailText.match(/\d{6}/)[0];
      await recoveryPage.getByLabel('6자리 이메일 확인 코드',{exact:true}).fill(code);await recoveryPage.getByRole('button',{name:'이메일 확인',exact:true}).click();await recoveryPage.getByRole('status').filter({hasText:'복구 이메일 확인을 완료했습니다'}).waitFor();
      await recoveryPage.getByRole('button',{name:'로그아웃',exact:true}).click();await recoveryPage.waitForURL(origin+'/platform');
      await recoveryPage.goto(origin+'/platform/login');await recoveryPage.getByRole('link',{name:'아이디·비밀번호 찾기',exact:true}).click();await recoveryPage.waitForURL(origin+'/platform/account/recovery');
      await recoveryPage.getByLabel('확인된 복구 이메일',{exact:true}).fill(recoveryAddress);await recoveryPage.getByRole('button',{name:'안내 메일 요청',exact:true}).click();
      await recoveryPage.getByRole('status').filter({hasText:'등록·확인된 복구 이메일'}).waitFor();
      const text=await mailbox(recoveryAddress,'메인 플랫폼 아이디 안내');const link=text.match(/http[^\s]+/)[0];assert.equal(new URL(link).pathname,'/platform/account/recovery');
      const oldLink=new URL(link);oldLink.pathname='/account/recovery';await recoveryPage.goto(oldLink.href);await recoveryPage.waitForURL(origin+'/platform/account/recovery');await recoveryPage.getByRole('button',{name:'계정 복구 확인',exact:true}).waitFor();assert.equal(new URL(recoveryPage.url()).hash,'');
      await recoveryPage.getByRole('button',{name:'계정 복구 확인',exact:true}).click();await recoveryPage.getByRole('status').filter({hasText:'가입 아이디: '+recoveryUser}).waitFor();
      await recoveryPage.goto(link);await recoveryPage.getByRole('button',{name:'계정 복구 확인',exact:true}).click();await recoveryPage.getByRole('alert').filter({hasText:'만료되었거나 사용할 수 없습니다'}).waitFor();
    });
    await stage('central_password_reset_security_management_and_withdrawal',async()=>{
      await recoveryPage.goto(origin+'/platform/account/recovery');await recoveryPage.getByLabel('찾을 정보',{exact:true}).selectOption('PASSWORD_RESET');await recoveryPage.getByLabel('확인된 복구 이메일',{exact:true}).fill(recoveryAddress);await recoveryPage.getByRole('button',{name:'안내 메일 요청',exact:true}).click({timeout:75000});
      await recoveryPage.getByRole('status').filter({hasText:'등록·확인된 복구 이메일'}).waitFor();const text=await mailbox(recoveryAddress,'메인 플랫폼 비밀번호 재설정');const link=text.match(/http[^\s]+/)[0];assert.equal(new URL(link).pathname,'/platform/account/recovery');
      const resetPassword='Reset'+randomBytes(5).toString('hex')+'!@';await recoveryPage.goto(link);await recoveryPage.getByLabel('새 비밀번호 (8~20자)',{exact:true}).fill(resetPassword);await recoveryPage.getByLabel('새 비밀번호 확인',{exact:true}).fill(resetPassword);await recoveryPage.getByRole('button',{name:'계정 복구 확인',exact:true}).click();await recoveryPage.getByRole('status').filter({hasText:'비밀번호를 재설정했습니다'}).waitFor();
      await login(recoveryPage,recoveryUser,'/platform/consumer',resetPassword);await recoveryPage.goto(origin+'/platform/account');await recoveryPage.getByText('비밀번호 변경',{exact:true}).first().click();
      const changedPassword='Change'+randomBytes(5).toString('hex')+'!@';await recoveryPage.getByLabel('현재 비밀번호',{exact:true}).fill(resetPassword);await recoveryPage.getByLabel('새 비밀번호 (8~20자)',{exact:true}).fill(changedPassword);await recoveryPage.getByLabel('새 비밀번호 확인',{exact:true}).fill(changedPassword);await recoveryPage.getByRole('button',{name:'비밀번호 변경',exact:true}).click();await recoveryPage.getByRole('status').filter({hasText:'비밀번호를 변경했습니다'}).waitFor();
      await recoveryPage.getByText('muther. 개인 저장 자료',{exact:true}).click();await recoveryPage.getByText('저장한 선곡표가 없습니다.',{exact:true}).waitFor();
      await recoveryPage.screenshot({path:evidence+'/10-central-account-security.png',fullPage:true});
      await recoveryPage.getByText('전체 계정 탈퇴',{exact:true}).click();await recoveryPage.getByLabel('탈퇴 확인용 현재 비밀번호',{exact:true}).fill(changedPassword);await recoveryPage.getByLabel('확인 문구 ‘전체 계정 탈퇴’ 입력',{exact:true}).fill('전체 계정 탈퇴');await recoveryPage.getByRole('button',{name:'공통 계정 탈퇴',exact:true}).click();await recoveryPage.waitForURL(origin+'/platform');
    });
    assert.deepEqual(errors,[]);assert.equal(requests.some(url=>url.includes('/api/admin/passkeys/')),false);
    fs.writeFileSync(evidence+'/summary.json',JSON.stringify({status:'pass',checks,js_errors:errors,passkey:'not_run_user_deferred',origin_kind:'private IPv4 HTTP; real browser secureContext=false'},null,2));
    console.log(JSON.stringify({status:'pass',checks,passkey:'not_run_user_deferred'}));
  }catch(error){fs.writeFileSync(evidence+'/failure.txt',error.stack);throw error;}finally{await browser.close();}
})().catch(()=>{console.error('Browser acceptance failed; inspect the private evidence file.');process.exit(1);});
