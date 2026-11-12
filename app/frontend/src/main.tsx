import {createRoot} from 'react-dom/client';
import {PublicPage} from './features/recommendation/PublicPage';
import {AdminPage,Login} from './features/admin/AdminPages';
import {AdsAdmin} from './features/admin-ads/AdsAdmin';
import {YoutubeAdmin} from './features/admin/YoutubeAdmin';
import './style.css';
import './features/admin-ads/ads-admin.css';
import './features/admin/admin-dashboard.css';
import {Signup,Recovery,MyPage,AdminPasskey} from './features/accounts/Accounts';
import {Platform} from './features/platform/Platform';
import {PlatformAlias,PublicAccountRoute} from './features/platform/PlatformAlias';
import {CinematicEntry} from './features/cinematic/CinematicEntry';
import {rootPlatformDestination} from './features/cinematic/entry';
import {VenueExplorePage} from './features/venues/pages/VenueExplorePage';
import {VenueClosureAdminPage} from './features/venues/pages/VenueClosureAdminPage';
const path=location.pathname;
const rootDestination=rootPlatformDestination(path,location.search,location.hash);
const accountFallback=path==='/platform/login'?<Login/>:path==='/platform/signup'?<Signup/>:path==='/platform/account'?<MyPage/>:path==='/platform/account/recovery'?<Recovery/>:null;
const platform=accountFallback?<PublicAccountRoute fallback={accountFallback}><Platform/></PublicAccountRoute>:<Platform/>;
if(rootDestination){
  location.replace(rootDestination);
}else{
  createRoot(document.getElementById('root')!).render(path==='/admin/venues/closures'?<PlatformAlias to="/platform/review?tab=closures"><VenueClosureAdminPage/></PlatformAlias>:path.startsWith('/venues')?<PlatformAlias to="/platform/consumer"><VenueExplorePage showVisitRecommendations/></PlatformAlias>:path==='/platform'||path.startsWith('/platform/')?<CinematicEntry>{platform}</CinematicEntry>:path==='/login'?<PlatformAlias to="/platform/login"><Login/></PlatformAlias>:path==='/signup'?<PlatformAlias to="/platform/signup"><Signup/></PlatformAlias>:path==='/account/recovery'?<PlatformAlias to="/platform/account/recovery"><Recovery/></PlatformAlias>:path==='/mypage'?<PlatformAlias to="/platform/account"><MyPage/></PlatformAlias>:path==='/admin/passkey'?<AdminPasskey/>:path==='/admin/music/youtube'?<YoutubeAdmin/>:path==='/admin/music'||path==='/admin/feedback/exclusions'?<AdminPage path={path}/>:path==='/admin/ads'?<AdsAdmin/>:<PublicPage/>);
}
