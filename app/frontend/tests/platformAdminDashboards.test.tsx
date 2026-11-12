import {describe,expect,it} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {PlatformAdminHome,type PlatformAdmin} from '../src/features/platform/PlatformSupport';
import {PlatformSidebar} from '../src/features/platform/Platform';
import {AdminLayout} from '../src/features/admin/AdminLayout';
import {AdsAdmin} from '../src/features/admin-ads/AdsAdmin';

const operations='/api/admin/operations/dashboard';
const behavior='/api/admin/operations/logs';
function access(path:string,overrides:Partial<{allowed:boolean;configured:boolean;path:string|null}>={}){
  return {allowed:true,configured:true,path,...overrides};
}
function admin(overrides:Partial<PlatformAdmin>={}):PlatformAdmin{
  return {role:'ADMIN',passkey_required:false,csrf_token:'test-only',platform_enabled:true,
    allowed_menus:[
      {path:'/admin/ads',label:'광고 관리'},
      {path:'/admin/music',label:'음악 관리'},
      {path:'/platform/review',label:'매장 권한 심사'},
      {path:'/api/admin/operations/kibana',label:'운영 대시보드'},
    ],
    operations_dashboard:access(operations),
    log_dashboard:access(behavior),
    ...overrides};
}
function render(value:PlatformAdmin|null){
  return renderToStaticMarkup(<PlatformAdminHome admin={value}/>);
}

describe('single platform administrator dashboard hub',()=>{
  it('shows precisely three main administrator dashboard destinations, followed by other tasks',()=>{
    const html=render(admin());
    expect(html).toContain('광고 대시보드');
    expect(html).toContain('사용자 행동 분석');
    expect(html).toContain('운영 대시보드');
    expect(html).toContain('href="/admin/ads"');
    expect(html).toContain('href="'+behavior+'"');
    expect(html).toContain('href="'+operations+'"');
    expect(html.match(/class="p-admin-dashboard-tile"/g)).toHaveLength(3);
    expect(html).toContain('음악 관리');
    expect(html).toContain('매장 권한 심사');
    expect(html).not.toContain('href="/api/admin/operations/kibana"');
  });

  it('never shows dashboards to non-admins, even if a server response were mistakenly populated',()=>{
    const html=render(admin({role:'USER'}));
    expect(html).toContain('href="/platform/admin/login"');
    expect(html).not.toContain('href="'+operations+'"');
    expect(html).not.toContain('href="'+behavior+'"');
    expect(html).not.toContain('href="/admin/ads"');
  });

  it('blocks all dashboard links while mandatory passkey step-up remains pending',()=>{
    const html=render(admin({passkey_required:true}));
    expect(html).toContain('추가 인증 대기');
    for(const path of [operations,behavior,'/admin/ads'])expect(html).not.toContain('href="'+path+'"');
  });

  it('shows the private review menu only when capability and menu are both present',()=>{
    expect(render(admin({platform_enabled:false,private_demo_review_allowed:true}))).toContain('href="/platform/review"');
    expect(render(admin({platform_enabled:false,private_demo_review_allowed:false}))).not.toContain('href="/platform/review"');
    expect(render(admin({platform_enabled:false,private_demo_review_allowed:true,allowed_menus:[]}))).not.toContain('href="/platform/review"');
  });

  it('does not expose a dashboard to an administrator lacking its capability',()=>{
    const html=render(admin({
      allowed_menus:[{path:'/platform/review',label:'매장 권한 심사'}],
      operations_dashboard:access(operations,{allowed:false}),
      log_dashboard:access(behavior,{allowed:false})
    }));
    expect(html).toContain('매장 권한 심사');
    expect(html).not.toContain('href="/admin/ads"');
    expect(html).not.toContain('href="'+operations+'"');
    expect(html).not.toContain('href="'+behavior+'"');
    expect(html).not.toContain('운영 대시보드');
    expect(html).not.toContain('사용자 행동 분석');
  });

  it('shows a disabled control rather than navigating if a dashboard is unconfigured',()=>{
    const html=render(admin({
      operations_dashboard:access(operations,{configured:false,path:null}),
      log_dashboard:access(behavior,{path:'/api/admin/operations/kibana'})
    }));
    expect(html).toContain('운영 대시보드');
    expect(html).toContain('사용자 행동 분석');
    expect((html.match(/aria-disabled="true"/g)||[]).length).toBe(2);
    expect(html).not.toContain('href="'+operations+'"');
    expect(html).not.toContain('href="'+behavior+'"');
    expect(html).toContain('href="/admin/ads"');
  });

  it('honors independent permissions for the two Kibana dashboards',()=>{
    const html=render(admin({
      allowed_menus:[],
      operations_dashboard:access(operations,{allowed:false}),
      log_dashboard:access(behavior)
    }));
    expect(html).toContain('href="'+behavior+'"');
    expect(html).not.toContain('href="'+operations+'"');
    expect(html).not.toContain('href="/admin/ads"');
    expect(html.match(/class="p-admin-dashboard-tile"/g)).toHaveLength(1);
  });

  it('hides dashboard destinations while no administrator is logged in',()=>{
    expect(render(null)).toContain('관리자 로그인이 필요합니다');
    for(const path of [operations,behavior,'/admin/ads'])expect(render(null)).not.toContain('href="'+path+'"');
  });
});

describe('legacy muther administrator headers are removed',()=>{
  it('music tool layout has only a return navigation to the platform administrator',()=>{
    const html=renderToStaticMarkup(
      <AdminLayout me={null} path="/admin/music" title="음악 관리" subtitle="음악 관리">
        <section>음악 목록</section>
      </AdminLayout>
    );
    expect(html).toContain('href="/platform/admin"');
    expect(html).toContain('음악 목록');
    expect(html).not.toContain('admin-dashboard-header');
    expect(html).not.toContain('날씨 기반');
    expect(html).not.toContain(operations);
    expect(html).not.toContain(behavior);
  });

  it('ad management is preserved and its legacy administrator header removed',()=>{
    const html=renderToStaticMarkup(<AdsAdmin/>);
    expect(html).toContain('광고 관리');
    expect(html).toContain('href="/platform/admin"');
    expect(html).not.toContain('admin-dashboard-header');
    expect(html).not.toContain('날씨 기반');
    expect(html).not.toContain(operations);
    expect(html).not.toContain(behavior);
    expect(html).toContain('href="/platform/admin/login"');
  });
});

describe('platform administrator sidebar navigation and Kibana guidance',()=>{
  it('renders /platform/admin and both configured Kibana dashboards in sidebar for authorized admins',()=>{
    const html=renderToStaticMarkup(<PlatformSidebar path="/platform/review" account={null} admin={admin()}/>);
    expect(html).toContain('href="/platform/admin"');
    expect(html).toContain('관리자 대시보드');
    expect(html).toContain('href="/platform/review"');
    expect(html).toContain('href="'+operations+'"');
    expect(html).toContain('운영 대시보드 (Kibana)');
    expect(html).toContain('href="'+behavior+'"');
    expect(html).toContain('사용자 행동 분석 (Kibana)');
    expect(html).toContain('href="/admin/ads"');
  });

  it('hides sidebar dashboards while passkey step-up is pending or for anonymous visitors',()=>{
    expect(renderToStaticMarkup(<PlatformSidebar path="/platform" account={null} admin={null}/>)).toBe('');
    const pendingHtml=renderToStaticMarkup(<PlatformSidebar path="/platform/admin" account={null} admin={admin({passkey_required:true})}/>);
    expect(pendingHtml).toContain('href="/platform/admin"');
    expect(pendingHtml).not.toContain('href="'+operations+'"');
    expect(pendingHtml).not.toContain('href="'+behavior+'"');
    expect(pendingHtml).not.toContain('href="/admin/ads"');
    expect(pendingHtml).not.toContain('href="/platform/review"');
  });

  it('shows Kibana env-var setup guidance when unconfigured and preserves closure shortcut',()=>{
    const html=render(admin({
      allowed_menus:[],
      operations_dashboard:access(operations,{configured:false,path:null}),
      log_dashboard:access(behavior,{allowed:false,configured:false,path:null}),
      venue_closure_allowed:true,
    }));
    expect(html).toContain('ADMIN_OPERATIONS_DASHBOARD_URL');
    expect(html).toContain('/platform/review?tab=closures');
  });
});
