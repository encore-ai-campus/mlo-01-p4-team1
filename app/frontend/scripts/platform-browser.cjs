/* Actual Compose HTTP + Chromium acceptance. Virtual WebAuthn is explicit. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const net=require('node:net');
const input=JSON.parse(fs.readFileSync('/mvp-private/local-inputs.json','utf8'));
const URL='http://localhost:5186';
const evidence='/evidence';
const checks=[],errors=[];
const proxy=http.createServer((request,response)=>{
  const upstream=http.request({hostname:'127.0.0.1',port:5173,path:request.url,method:request.method,headers:request.headers},result=>{
    response.writeHead(result.statusCode,result.headers);result.pipe(response);
  });
  upstream.on('error',()=>{response.writeHead(502);response.end();});request.pipe(upstream);
});
proxy.on('upgrade',(request,socket,head)=>{
  const upstream=net.connect(5173,'127.0.0.1',()=>{
    upstream.write(request.method+' '+request.url+' HTTP/1.1\r\n'+Object.entries(request.headers).map(([k,v])=>k+': '+v).join('\r\n')+'\r\n\r\n');
    if(head.length)upstream.write(head);socket.pipe(upstream).pipe(socket);
  });upstream.on('error',()=>socket.destroy());socket.on('error',()=>upstream.destroy());
});
async function stage(name,work){console.log('RUN '+name);await work();checks.push(name);}
async function pageFor(browser){const context=await browser.newContext({viewport:{width:1440,height:1000}});const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));return {context,page};}
async function login(page,username,administrator=false){
  await page.goto(URL+(administrator?'/platform/admin/login':'/platform/login'));
  await page.getByLabel('로그인 아이디',{exact:true}).fill(username);
  await page.getByLabel('로그인 비밀번호',{exact:true}).fill(input.password);
  await page.getByRole('button',{name:'로그인',exact:true}).click();
  await page.waitForURL(administrator?/\/(admin\/passkey|platform\/review)$/:/\/platform\/(business|consumer|$)/);
}
async function api(page,path,method='GET',body,extra={}){
  return page.evaluate(async({path,method,body,extra})=>{
    const base='/api/platform/v1';const boot=await(await fetch(base+'/sessions/current')).json();
    const csrf=boot.data.session?.csrf_token||boot.data.csrf_token;
    const result=await fetch(base+path,{method,headers:{'Content-Type':'application/json','X-CSRFToken':csrf,'Idempotency-Key':crypto.randomUUID(),...extra},...(body===undefined?{}:{body:JSON.stringify(body)})});
    return {status:result.status,body:await result.json()};
  },{path,method,body,extra});
}
async function until(work,message){for(let i=0;i<40;i++){if(await work())return;await new Promise(r=>setTimeout(r,250));}throw new Error(message);}

(async()=>{
  fs.mkdirSync(evidence,{recursive:true});await new Promise(r=>proxy.listen(5186,'127.0.0.1',r));
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try{
    const anonymous=await pageFor(browser);
    await stage('home_and_mobile_layout',async()=>{
      await anonymous.page.goto(URL+'/platform');await anonymous.page.locator('.p-hero').waitFor();
      await anonymous.page.screenshot({path:evidence+'/01-home.png',fullPage:true});
      await anonymous.page.setViewportSize({width:390,height:844});
      assert(await anonymous.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
      await anonymous.page.screenshot({path:evidence+'/02-home-mobile.png',fullPage:true});
      await anonymous.page.setViewportSize({width:1440,height:1000});
    });
    const consumer=await pageFor(browser);
    await stage('consumer_signup_then_explicit_business_type_add',async()=>{
      await consumer.page.goto(URL+'/platform/signup');
      await consumer.page.getByLabel('가입 아이디',{exact:true}).fill('qa'+Date.now().toString(36));
      await consumer.page.getByLabel('가입 비밀번호',{exact:true}).fill(input.password);
      await consumer.page.getByLabel('가입 비밀번호 확인',{exact:true}).fill(input.password);
      for(const text of ['[필수] 서비스 이용약관에 동의합니다.','[필수 확인] 기본 회원정보 처리 안내를 확인했습니다.','[필수 확인] 만 14세 이상입니다.'])await consumer.page.getByLabel(text,{exact:true}).check();
      await consumer.page.screenshot({path:evidence+'/03-signup.png',fullPage:true});
      await consumer.page.getByRole('button',{name:'회원가입하고 시작하기'}).click();await consumer.page.waitForURL('**/platform/consumer');
      await consumer.page.getByRole('heading',{name:'사용자 서비스를 준비하고 있습니다'}).waitFor();
      await consumer.page.goto(URL+'/platform/account');
      await consumer.page.getByLabel('약관에 동의하고 처리 안내를 확인했습니다.').check();
      await consumer.page.getByRole('button',{name:'사업자 유형 추가',exact:true}).click();
      await consumer.page.waitForURL('**/platform/business');
    });
    const owner=await pageFor(browser);await login(owner.page,'mvpowner');
    let currentStore;
    await stage('owner_claim_queued_synthetic_check_and_submission',async()=>{
      const prior=await api(owner.page,'/me/workspaces');
      if(prior.body.data.items.length){currentStore=prior.body.data.items[0];return;}
      const claims=await api(owner.page,'/me/workspace-claims');
      if(claims.body.data.items.some(c=>c.state==='SUBMITTED'))return;
      if(!claims.body.data.items.some(c=>['DRAFT','SUBMITTED'].includes(c.state))){
        await owner.page.getByRole('button',{name:'초안 저장 · 합성 정보 확인',exact:true}).click();
        await owner.page.getByRole('status').filter({hasText:'초안 저장'}).waitFor();
      }
      await until(async()=>{
        await owner.page.getByRole('button',{name:'새로고침',exact:true}).click();
        const rows=await api(owner.page,'/me/workspace-claims');return rows.body.data.items.some(c=>c.match_result==='MATCHED')&&await owner.page.getByRole('button',{name:'심사 제출',exact:true}).first().isEnabled();
      },'synthetic check worker did not complete');
      const pending=(await api(owner.page,'/me/workspace-claims')).body.data.items.find(c=>c.state==='SUBMITTED');
      if(!pending){await owner.page.getByRole('button',{name:'심사 제출',exact:true}).first().click();await owner.page.getByText('심사 대기',{exact:true}).waitFor();}
    });
    const reviewer=await pageFor(browser);
    const cdp=await reviewer.context.newCDPSession(reviewer.page);
    await cdp.send('WebAuthn.enable');
    const {authenticatorId}=await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}});
    const credentialsFile='/mvp-private/qa-passkey.json';
    if(fs.existsSync(credentialsFile))for(const credential of JSON.parse(fs.readFileSync(credentialsFile,'utf8'))){
      // Restored virtual state starts at the next counter, not a replay of the last assertion.
      credential.signCount+=1;
      await cdp.send('WebAuthn.addCredential',{authenticatorId,credential});
    }
    await stage('reviewer_primary_login_and_real_virtual_webauthn',async()=>{
      await login(reviewer.page,'mvpqareviewer',true);
      if(new globalThis.URL(reviewer.page.url()).pathname==='/admin/passkey'){
        if(fs.existsSync(credentialsFile))await reviewer.page.getByRole('button',{name:'패스키로 인증',exact:true}).click();
        else{
          await reviewer.page.getByLabel('최초 등록·분실 복구용 등록권').fill(input.qa_enrollment_ticket);
          await reviewer.page.getByRole('button',{name:'패스키 등록',exact:true}).click();
        }
        await reviewer.page.waitForURL('**/platform/review');
        const credentials=await cdp.send('WebAuthn.getCredentials',{authenticatorId});fs.writeFileSync(credentialsFile,JSON.stringify(credentials.credentials),{mode:0o600});
      }
    });
    await stage('reviewer_approval_atomically_grants_workspace_owner',async()=>{
      if(!currentStore){
        await reviewer.page.getByRole('heading',{name:'승인 대기 신청'}).waitFor();
        await reviewer.page.screenshot({path:evidence+'/04-review.png',fullPage:true});
        await reviewer.page.locator('.p-review').filter({hasText:'합성 시연 카페'}).getByRole('button',{name:'승인',exact:true}).click();
        await reviewer.page.getByRole('status').filter({hasText:'심사 결과'}).waitFor();
        await owner.page.getByRole('button',{name:'새로고침',exact:true}).click();
        currentStore=(await api(owner.page,'/me/workspaces')).body.data.items[0];
      }
      assert(currentStore);await owner.page.locator('.p-store').first().click();
      await owner.page.getByRole('heading',{name:/음악 운영$/}).waitFor();
    });
    const staff=await pageFor(browser);await login(staff.page,'mvpstaff');
    await stage('owner_invites_account_bound_staff_and_staff_accepts',async()=>{
      const existing=await api(staff.page,'/me/workspaces');
      if(!existing.body.data.items.length){
        await staff.page.getByRole('button',{name:'초대 수신 코드 발급',exact:true}).click();
        const code=await staff.page.locator('.p-code code').innerText();
        await owner.page.getByLabel('초대 수신 코드',{exact:true}).fill(code);
        await owner.page.getByLabel('소유자 비밀번호 재확인',{exact:true}).fill(input.password);
        await owner.page.getByRole('button',{name:'초대 생성',exact:true}).click();
        await owner.page.getByRole('status').filter({hasText:'초대를 보냈습니다'}).waitFor();
        await staff.page.getByRole('button',{name:'초대함 새로고침',exact:true}).click();
        await staff.page.getByRole('button',{name:'초대 수락',exact:true}).click();
        await staff.page.getByRole('status').filter({hasText:'초대를 수락했습니다'}).waitFor();
      }
      await staff.page.locator('.p-store').first().click();
      await staff.page.getByRole('heading',{name:/음악 운영$/}).waitFor();
      assert.equal(await staff.page.getByRole('button',{name:'초대 생성',exact:true}).count(),0);
    });
    await stage('staff_final_approval_exclusions_and_music_settings',async()=>{
      await staff.page.getByRole('button',{name:'최종 승인 · 매장 적용',exact:true}).first().click();
      await staff.page.getByRole('status').filter({hasText:'매장에 적용했습니다'}).waitFor();
      await staff.page.getByRole('button',{name:/합성 곡 1 제외/}).click();
      await until(async()=>!(await staff.page.getByRole('button',{name:'음악 분위기 변경',exact:true}).isDisabled()),'music update did not finish');
      await staff.page.getByRole('button',{name:'음악 분위기 변경',exact:true}).click();
      await until(async()=>!(await staff.page.getByRole('button',{name:'음악 분위기 변경',exact:true}).isDisabled()),'preferences update did not finish');
      await staff.page.screenshot({path:evidence+'/05-staff-music.png',fullPage:true,mask:[staff.page.locator('.p-code')]});
      await owner.page.locator('.p-store').first().click();
      await owner.page.screenshot({path:evidence+'/06-owner-workspace.png',fullPage:true});
    });
    await stage('cross_workspace_and_staff_invite_are_denied_by_api',async()=>{
      const foreign=await api(consumer.page,`/workspaces/${currentStore.id}`);assert.equal(foreign.status,404);
      const denial=await api(staff.page,`/workspaces/${currentStore.id}/invitations`,'POST',{recipient_code:'synthetic-invalid-code-000','role':'STAFF'},{'If-Match':'"1"'});assert.equal(denial.status,403);
    });
    assert.deepEqual(errors,[]);
    fs.writeFileSync(evidence+'/browser-summary.json',JSON.stringify({status:'pass',checks,js_errors:errors,authenticator:'Chromium virtual WebAuthn; not physical-device acceptance'},null,2));
    console.log(JSON.stringify({status:'pass',checks}));
  }catch(error){
    fs.writeFileSync(evidence+'/browser-failure.txt',error.stack);throw error;
  }finally{await browser.close();proxy.closeAllConnections();proxy.close();}
})().catch(error=>{console.error(error.message);process.exit(1);});
