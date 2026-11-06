# 사계절 시네마틱 — 플랫폼 진입 통합

승인된 32초 봄 → 여름 → 가을 → 겨울 → 입체 로고/브랜딩 연출을 플랫폼 첫 진입에 연결합니다. 신규 인트로 모듈, 기존 프런트 진입점과 헤더의 페이지 이동 링크만 연결하며, 회원·로그인·복구·매장·관리자 권한 로직은 변경하지 않습니다.

## 진입 규칙

| 경로·행동 | 결과 |
|---|---|
| 새 브라우저 탭의 `/platform` 또는 `/platform/` | 사계절 시네마틱을 먼저 표시 |
| `/platform/intro` 또는 `/platform?cinematic=replay` | 인트로를 명시적으로 다시 재생 |
| 플랫폼 상단 서비스 소개 왼쪽 `시네마틱 페이지로 이동` | `/platform/intro`로 이동; 종료 후 자동 복귀하지 않음 |
| 좌상단 메인 플랫폼 로고 | 즉시 기존 플랫폼 홈으로 이동 |
| 마지막 `메인플랫폼 바로가기 >>` | 기존 플랫폼 홈으로 이동 |
| `SKIP` 또는 Escape | 승인된 동작대로 마지막 브랜딩 장면을 표시; 자동 서비스 이동 아님 |
| `REPLAY` / 사계절 다시 보기 | 인트로를 처음부터 재생 |
| 같은 탭에서 홈 재방문·새로고침 | 완료한 인트로를 반복하지 않음 |
| 로그인·가입·복구·마이페이지·관리자·법적 안내 직접 링크 | 인트로를 삽입하지 않고 기존 화면을 그대로 표시 |
| `/platform?cinematic=skip` | 화면 표시만 건너뜀. 계정·권한 검사를 우회하지 않음 |

재생 완료 자체로 메인 플랫폼에 자동 이동하거나 원래 페이지로 자동 복귀하지 않습니다. 마지막 브랜딩 화면에 머물며, 사용자가 로고 또는 메인플랫폼 바로가기 링크를 선택할 때만 이동합니다.

첫 진입 여부는 `sessionStorage`의 `mlo:seasonal-intro:seen:v1=1`로만 관리합니다. 개인정보·로그인 상태·토큰은 보관하지 않습니다. 직접 로그인·가입 화면으로 들어온 탭에서도 이후 홈으로 돌아갈 때 인트로가 끼어들지 않도록 표시 상태를 기록합니다. 브라우저가 저장소를 차단하면 명시적 `cinematic=skip` 쿼리를 이용하여 홈 이동의 반복 재생을 피합니다. 기존 탭에서 복제한 새 탭의 sessionStorage는 브라우저 동작에 따라 복사될 수 있으므로 매번 다시 볼 때는 명시적인 replay 경로를 사용합니다.

인트로의 링크는 현재 origin의 `/platform`으로 이동합니다. 개발용 LAN IP, 특정 포트, 독립 프리뷰 서버 주소에 의존하지 않습니다. 기존 쿼리와 `#services` 앵커는 보존하고, 재생 제어용 쿼리는 홈 진입 시 정리합니다.

## 파일과 격리

- `../src/features/cinematic/CinematicEntry.tsx`: 기존 `<Platform/>`의 바깥에서 최초 진입을 결정합니다.
- `../src/features/cinematic/entry.ts`: 경로·표시 상태·메시지 검증 함수입니다.
- `../src/features/cinematic/template.html`: 승인된 화면 구조입니다.
- `scene.js`, `style.css`: 승인된 Three.js 장면과 스타일 원본입니다.
- `build.mjs`, `package.json`, `package-lock.json`: 앱과 분리한 고정 의존성 빌더입니다.
- `../public/assets/cinematic/`: 동일 origin에서 제공하는 해시 기반 JS/CSS입니다.
- `../src/features/cinematic/generated.ts`: 생성한 정적 파일 경로입니다.
- `../tests/cinematicEntry.test.ts`: 진입·기존 경로 보존·자산·라이선스 검증입니다.

첫 화면은 신뢰할 수 있는 프로젝트 내부 HTML만으로 구성한 `srcDoc` iframe입니다. 인트로의 전역 CSS, DOM 이벤트, WebGL 및 오디오가 기존 React 화면과 섞이지 않도록 분리합니다. iframe에서 보내는 완료 메시지는 같은 origin, 현재 iframe의 Window, 고정된 메시지 종류와 버전을 모두 확인합니다. 단순히 페이지에 같은 메시지를 보내는 것으로 인증이나 권한을 바꾸지 않습니다.

로고와 마지막 버튼에는 JavaScript 메시지 처리 외에도 고정된 플랫폼 홈 링크가 있어 기본 탐색을 지원합니다. 인트로를 종료하면 iframe을 제거하며 animation frame, 오디오와 GPU 자원을 정리합니다. WebGL 초기화 실패 시 정적 로고와 홈 진입을 제공하고, 로딩 지연 시 별도 진입 버튼을 제공합니다. OS 동작 줄이기 설정은 브랜딩 화면을 먼저 보여줍니다. 배경음은 기본 무음입니다.

## 빌드와 검증

일반 앱의 `package.json`·pnpm lock을 바꾸지 않습니다. 생성된 JS/CSS는 저장소에 포함되어 기존 Vite 및 이미지 빌드에 함께 들어가므로, 일반 앱 실행 시 별도의 npm 설치나 CDN 연결이 필요하지 않습니다.

인트로 원본을 수정했을 때만 이 디렉터리에서 실행합니다.

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run build
npm run check
```

Three.js `0.180.0`, esbuild `0.28.2`를 고정했습니다. Three.js MIT 라이선스는 `THREE-LICENSE.txt`와 배포 JS 배너에 보존합니다. 변경된 자산에는 새 내용 해시를 사용합니다. 이전 해시의 파일을 정리할 때는 구버전 HTML·탭·배포와의 호환을 확인하며, 다른 담당자의 정적 파일을 삭제하지 않습니다.

일반 프런트엔드 환경에서는 다음 검사를 적용합니다.

```bash
# app/frontend에서, 프로젝트 의존성이 준비된 실행 환경
npm test
npm run build
```

Vite는 `public` 파일을 그대로 배포 출력에 복사합니다. 생성 자산을 `/assets/cinematic/` 아래 두어 현재 Django의 `/assets/<path>` 정적 제공 경로도 활용합니다. 별도 Django URL·회원 API·Docker Compose 수정은 필요하지 않습니다.

## 동시 작업 인계

기존 파일 수정은 `src/main.tsx`에 대한 import 1개와 Platform 분기의 wrapper 1개, `features/platform/Platform.tsx`의 헤더 탐색 링크용 import와 `CinematicNavLink` 삽입뿐입니다. 다른 에이전트가 이 파일을 변경할 때 인트로 wrapper를 유지하되, 기존 PlatformAlias 및 신규 인증 경로는 그대로 보존합니다. `Platform.tsx`의 헤더 링크 외 회원/로그인·가입 로직, `features/accounts/`, 공용 API 클라이언트, 백엔드, DB는 수정하지 않습니다.

기존 UI 검증에서 플랫폼 홈 자체만 검사하려면 `/platform?cinematic=skip`을 사용합니다. 인트로 E2E는 별도 깨끗한 브라우저 컨텍스트 또는 replay 경로에서 수행합니다. 인트로를 건너뛰는 것은 표시 계층에만 영향을 주므로 로그인·권한 검증을 생략하는 수단으로 쓰지 않습니다.

## 검증 범위

데스크톱 Chrome의 실제 WebGL 동작, 주요 조작, 전체 자연 재생, 계정 관련 직접 경로, 모바일 화면 크기 에뮬레이션, Vite production build를 확인합니다. 실제 모바일 기기의 GPU·배터리, Safari 및 실제 AWS 배포 검증은 별도입니다. 이 변경 자체가 로그인·가입의 전체 수용 시험 또는 AWS 배포 완료를 뜻하지 않습니다.

공식 참고: [Vite 정적 자산](https://vite.dev/guide/assets), [esbuild API](https://esbuild.github.io/api/), [Three.js](https://threejs.org/).
