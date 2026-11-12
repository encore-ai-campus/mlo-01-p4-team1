import {createRoot} from 'react-dom/client';
import {PublicPage} from './features/recommendation/PublicPage';
import {AdminPage} from './features/admin/AdminPages';
import {AdsAdmin} from './features/admin-ads/AdsAdmin';
import {YoutubeAdmin} from './features/admin/YoutubeAdmin';
import './style.css';
import './features/admin-ads/ads-admin.css';
import './features/admin/admin-dashboard.css';
import {Signup,Recovery,MyPage,AdminPasskey} from './features/accounts/Accounts';
import {Platform} from './features/platform/Platform';
import {PlatformAlias} from './features/platform/PlatformAlias';
import {CinematicEntry} from './features/cinematic/CinematicEntry';
import {rootPlatformDestination} from './features/cinematic/entry';
import {VenueExplorePage} from './features/venues/pages/VenueExplorePage';
import {VenueClosureAdminPage} from './features/venues/pages/VenueClosureAdminPage';
const path=location.pathname;
const rootDestination=path==='/login'?'/platform/login'+location.search+location.hash:rootPlatformDestination(path,location.search,location.hash);
if(rootDestination){
  location.replace(rootDestination);
}else{
  createRoot(document.getElementById('root')!).render(path==='/admin/venues/closures'?<PlatformAlias to="/platform/review?tab=closures"><VenueClosureAdminPage/></PlatformAlias>:path.startsWith('/venues')?<PlatformAlias to="/platform/consumer"><VenueExplorePage showVisitRecommendations/></PlatformAlias>:path==='/platform'||path.startsWith('/platform/')?<CinematicEntry><Platform/></CinematicEntry>:path==='/signup'?<PlatformAlias to="/platform/signup"><Signup/></PlatformAlias>:path==='/account/recovery'?<PlatformAlias to="/platform/account/recovery"><Recovery/></PlatformAlias>:path==='/mypage'?<PlatformAlias to="/platform/account"><MyPage/></PlatformAlias>:path==='/admin/passkey'?<AdminPasskey/>:path==='/admin/music/youtube'?<YoutubeAdmin/>:path==='/admin/music'||path==='/admin/feedback/exclusions'?<AdminPage path={path}/>:path==='/admin/ads'?<AdsAdmin/>:<PublicPage/>);
}
