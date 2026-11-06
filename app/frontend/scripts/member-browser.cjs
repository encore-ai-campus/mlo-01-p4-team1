/* Actual local UI, SMTP and virtual WebAuthn acceptance; fixtures are never logged. */
const {chromium}=require('playwright');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const f=JSON.parse(fs.readFileSync('/test-private/fixture.json','utf8'));
const base='http://localhost:5173';
const checks=[];
const evidence='/evidence';
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function waitFor(run){let last;for(let i=0;i<50;i++){try{const r=await run();if(r)return r;}catch(e){last=e;}await pause(300);}throw new Error('condition timeout');}
async function latestMail(subject){
  return waitFor(async()=>{const r=await fetch('http://mailpit:8025/api/v1/messages');const list=await r.json();
    const m=list.messages.find(m=>m.Subject===subject&&m.To.some(to=>to.Address===f.email));
    if(!m)return null;return (await (await fetch('http://mailpit:8025/api/v1/message/'+m.ID)).json()).Text;});
}
async function main(){
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const context=await browser.newContext({viewport:{width:1280,height:1000}});
  const page=await context.newPage();
  page.setDefaultTimeout(20000);
  fs.mkdirSync(evidence+'/screenshots',{recursive:true});
  async function login(username,password=f.password){
    await page.goto(base+'/login');await page.locator('#username').fill(username);await page.locator('#password').fill(password);
    await page.getByRole('button',{name:'로그인',exact:true}).click();
    await page.waitForURL(url=>url.pathname!='/login');
  }
  async function post(path,body){const me=await (await context.request.get(base+'/api/auth/me')).json();
    return context.request.post(base+path,{data:body,headers:{'X-CSRFToken':me.csrf_token}});}
  async function signup(username,email=false){
    await page.goto(base+'/signup');await page.locator('#signup-username').fill(username);
    await page.getByRole('button',{name:'중복 확인'}).click();await page.getByText('사용 가능한 아이디입니다.',{exact:true}).waitFor();
    await page.locator('#signup-password').fill(f.password);await page.locator('#signup-confirm').fill(f.password);
    await page.getByLabel('[필수] 서비스 이용약관 동의',{exact:true}).check();
    await page.getByLabel('[확인] 기본 회원정보 처리 안내 확인',{exact:true}).check();
    await page.getByLabel('만 14세 이상입니다.',{exact:true}).check();
    if(email){await page.getByLabel('[선택] 계정 복구용 이메일 수집·이용 동의',{exact:true}).check();await page.locator('#signup-email').fill(f.email);}
    await page.getByRole('button',{name:'회원가입',exact:true}).click();
    await page.getByRole('status').filter({hasText:'가입이 완료되었습니다.'}).waitFor();
  }
  try{
    await waitFor(async()=>{const r=await context.request.get(base+'/api/accounts/policy');return r.ok();});
    await page.goto(base+'/signup');
    await page.screenshot({path:evidence+'/screenshots/signup-desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:evidence+'/screenshots/signup-mobile.png',fullPage:true});
    await page.setViewportSize({width:1280,height:1000});
    await signup(f.member);
    const created=(await page.getByRole('status').innerText()).match(/회원\d{4}/);
    assert.ok(created);
    await login(f.member.toUpperCase());
    await page.goto(base+'/mypage');await page.locator('#profile-nickname').waitFor();
    await page.locator('#profile-nickname').fill('산들바람');await page.getByRole('button',{name:'별명 변경',exact:true}).click();
    await page.getByRole('status').filter({hasText:'별명을 변경했습니다.'}).waitFor();
    const profile=await (await context.request.get(base+'/api/me/profile')).json();assert.equal(profile.nickname,'산들바람');assert.equal(profile.email_verified,false);
    checks.push('email_optional_signup_generated_unique_nickname_profile_and_casefold_login');
    await post('/api/auth/logout',{});
    await signup(f.member_email,true);
    await login(f.member_email);await page.goto(base+'/mypage');
    const text=await latestMail('복구 이메일 확인');const code=text.match(/\d{6}/)[0];
    await page.locator('#email-code').fill(code);await page.getByRole('button',{name:'복구 이메일 확인',exact:true}).click();
    await page.getByRole('status').filter({hasText:'복구 이메일 확인을 완료했습니다.'}).waitFor();
    await page.screenshot({path:evidence+'/screenshots/mypage-desktop.png',fullPage:true});
    assert.equal((await (await context.request.get(base+'/api/me/profile')).json()).email_verified,true);
    checks.push('actual_smtp_code_delivery_and_optional_email_confirmation');
    await post('/api/auth/logout',{});
    await page.goto(base+'/account/recovery');await page.locator('#recovery-purpose').selectOption('password');await page.locator('#recovery-email').fill(f.email);
    await page.getByRole('button',{name:'안내 메일 요청'}).click();await page.getByRole('status').waitFor();
    const reset=await latestMail('비밀번호 재설정');const link=reset.match(/http:\/\/localhost:5173\/account\/recovery#[^\s]+/)[0];
    await page.goto(link);await page.locator('#reset-password').fill(f.password+'new');await page.locator('#reset-confirm').fill(f.password+'new');
    assert.equal(new URL(page.url()).hash,'');
    await page.getByRole('button',{name:'확인',exact:true}).click();await page.getByRole('status').filter({hasText:'비밀번호를 재설정했습니다.'}).waitFor();
    assert.equal((await (await context.request.get(base+'/api/auth/me')).json()).authenticated,false);
    await login(f.member_email,f.password+'new');await page.goto(base+'/mypage');
    await page.locator('#withdraw-password').fill(f.password+'new');await page.locator('#withdraw-confirm').fill('회원탈퇴');
    await page.getByRole('button',{name:'회원탈퇴',exact:true}).click();await page.waitForURL(url=>url.pathname==='/');
    assert.equal((await (await context.request.get(base+'/api/auth/me')).json()).authenticated,false);
    checks.push('password_recovery_fragment_token_normal_login_and_withdrawal');
    const cdp=await context.newCDPSession(page);await cdp.send('WebAuthn.enable');
    await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
    await login(f.admin);await page.waitForURL(base+'/admin/passkey');
    assert.equal((await context.request.get(base+'/api/admin/feedback/metrics')).status(),403);
    await page.locator('#enrollment-ticket').fill(f.ticket);await page.getByRole('button',{name:'패스키 등록',exact:true}).click();
    await page.waitForURL(base+'/admin/music');assert.equal((await (await context.request.get(base+'/api/auth/me')).json()).role,'ADMIN');
    checks.push('virtual_passkey_enrollment_after_primary_and_management_denial_before_stepup');
    await post('/api/auth/logout',{});await login(f.admin);await page.waitForURL(base+'/admin/passkey');
    await page.getByRole('button',{name:'패스키로 인증',exact:true}).click();await page.waitForURL(base+'/admin/music');
    checks.push('virtual_passkey_signature_authentication_on_next_login');
    fs.writeFileSync(evidence+'/browser-summary.json',JSON.stringify({status:'pass',checks},null,2));
    console.log(JSON.stringify({status:'pass',checks}));
  }catch(error){await page.screenshot({path:evidence+'/screenshots/failure.png',fullPage:true}).catch(()=>{});console.error('browser_acceptance_failed',error.name);throw error;}
  finally{await browser.close();}
}
main().catch(()=>process.exit(1));
