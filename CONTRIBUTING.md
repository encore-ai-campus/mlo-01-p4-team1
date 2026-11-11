# MLO 4차 프로젝트 GitHub 협업 운영 가이드

**적용 대상:** 프로젝트 팀원 전체

**목적:** Issue·Commit·PR·리뷰·릴리스를 연결해 개발 이력을 추적한다.

**상태:** 운영 기준 도입안. 이 문서 저장만으로 GitHub 설정·브랜치 보호가 적용되지는 않는다.

팀원은 각자 사용하는 IDE·터미널·Git 도구로 작업한다. 특정 개발 도구 설치나 개인의 병렬 작업 환경은 팀 공통 요구 사항이 아니다.

## 1. 기본 개발 절차

**Issue 생성 → 담당자·범위 지정 → 작업 브랜치 생성 → 개발·검증 → Issue 번호 포함 Commit → Push → PR → 리뷰 → Merge → 릴리스 후 Issue 종료**

프론트엔드·백엔드·데이터·모델·인프라·문서 작업에 동일하게 적용한다. 큰 작업은 상위 Issue와 독립적으로 개발·검증할 수 있는 하위 Issue로 나눈다.

코드 구현과 실제 운영 검증은 완료 기준이 다르면 별도 Issue로 관리한다. 예를 들어 인프라 코드의 로컬 검증 완료와 실제 AWS 배포 성공, 모델 중계 코드 구현과 집 PC GPU 연결 성공을 구분한다.

## 2. Issue 작성 규칙

제목은 작업 유형과 목적이 드러나도록 작성한다.

```text
[Feature] 날씨 기반 음악 추천 요청 기능 구현
[Bug] Gateway 전환 시 동일 요청 중복 추론 방지
[Task] 5계정 Terraform 구성 및 로컬 검증
[Task] AWS와 집 PC 모델 API 실제 연결 검증
[Docs] 인프라 배포·복구 가이드 작성
```

Issue에는 다음을 기록한다.

- 목적과 기대 결과
- 수정 범위·제외 범위·영향받는 기능이나 디렉터리
- 책임을 맡는 팀원 1명(Assignee)
- 기준 브랜치와 선행 Issue·계약 의존성
- 완료 기준과 검증 명령·예상 결과
- 데이터·배포 영향과 필요한 외부 실행 승인

담당자가 여러 명 필요한 큰 작업은 가능한 한 하위 Issue로 나누고 각 담당자를 정한다.

라벨은 저장소에 실제로 생성한 뒤 사용한다.

| 구분 | 예시 |
|---|---|
| 유형 | `type:feature`, `type:bug`, `type:task`, `type:docs` |
| 영역 | `area:frontend`, `area:backend`, `area:data`, `area:infra`, `area:llm` |
| 인프라 세부 영역 | `component:foundation`, `component:runtime`, `component:lifecycle` |

상세 상태는 Issue 본문이나 팀 Project에서 `대기 → 개발 중 → 리뷰 중 → 통합 완료·릴리스 대기 → 종료`로 추적한다. Project가 없으면 Issue 본문으로 관리하며, 설정되지 않은 자동화를 동작 중이라고 표시하지 않는다.

## 3. 브랜치와 PR 대상

| 브랜치 | 역할 | PR 대상 |
|---|---|---|
| `main` | 검증된 릴리스 기준·GitHub 기본 브랜치 | — |
| `develop` | 팀 전체 개발 통합 | `main` |
| `feature/infra` | 인프라 작업 통합 | `develop` |
| 일반 작업 브랜치 | 개별 Issue 구현 | `develop` |
| 인프라 작업 브랜치 | 개별 인프라 Issue 구현 | `feature/infra` |

일반 작업은 최신 `develop`, 인프라 작업은 합의된 `feature/infra` 기준에서 시작한다. 하나의 작업 Issue에는 하나의 작업 브랜치를 기본으로 사용한다. `feature/infra`는 여러 인프라 작업을 모으는 통합 브랜치다.

신규 브랜치 이름은 다음 형식을 사용한다.

```text
<type>/<issue-number>-<description>
```

```text
feat/42-weather-feedback
fix/43-gateway-request-dedup
chore/44-infra-lifecycle
docs/45-infra-runbook
ci/46-main-web-pipeline
```

이 문서의 Issue 번호는 예시다. 실제 발급된 번호로 바꿔 사용한다.

일반 작업 브랜치 생성 예시:

```bash
git fetch origin
git switch develop
git pull --ff-only origin develop
git switch -c feat/42-weather-feedback
```

위 명령은 원격과 기준 브랜치가 준비되고 현재 변경을 확인한 작업 경로에서 사용한다. 미커밋 변경을 버리거나 다른 작업 경로의 브랜치를 강제로 변경하지 않는다.

기존 브랜치는 이번 전환 작업에 한해 이름을 유지하고 해당 Issue에 연결할 수 있다. 기존 커밋에 번호를 붙이기 위해 이력을 임의로 재작성하지 않는다. 이후 신규 작업부터 번호 규칙을 적용한다.

## 4. Commit 규칙

모든 신규 작업 커밋에 해당 Issue 번호를 포함한다.

```text
type(scope): 한국어 변경 내용 (#이슈번호)
```

```text
feat(infra): 5계정 네트워크와 서버 구성 구현 (#42)
fix(runtime): 상태 저장 서비스 롤백의 백업 검사 보완 (#43)
feat(lifecycle): 인프라 삭제 계획과 실행 절차 구현 (#44)
test(llm): 중계 장애 전환과 요청 중복 방지 검증 (#45)
docs(infra): 배포와 복구 절차 정리 (#46)
```

기본 타입은 `feat`, `fix`, `refactor`, `test`, `docs`, `chore`다. scope는 영향받는 기능이나 구성요소를 사용한다.

- 한 커밋은 가능한 한 하나의 목적과 Issue에 집중한다.
- 서로 무관한 공통 기준·구현·문서 변경을 한 커밋에 섞지 않는다.
- 파일과 staged diff를 확인하고 담당 범위만 staging한다.
- 메시지는 실제 변경 내용 중심으로 작성하고 작업 도구·자동 작성자 표기를 넣지 않는다.
- `(#42)`는 참조 표기이며 Issue를 자동 종료하지 않는다.

이미 진행한 미커밋 작업은 대응 Issue를 만들고 기존 작업·검증 이력을 정직하게 기록한 뒤 커밋한다. Issue 생성 시점보다 이전 작업을 이후에 수행한 것처럼 기록하지 않는다.

## 5. PR·검증·리뷰

개발과 해당 범위의 검증을 완료한 뒤 자기 작업 브랜치를 push하고 올바른 대상으로 PR을 생성한다. 진행 중 검토가 필요하면 Draft PR을 사용한다.

PR 제목에도 Issue 번호를 포함한다.

```text
fix(runtime): 상태 저장 서비스 롤백 보호 보완 (#43)
```

PR 본문 예시:

```markdown
## 관련 Issue
Related #43

## 문제와 변경
- 기존 문제:
- 변경 후 동작:
- 수정 범위 / 제외 범위:

## 검증
- 실행 명령:
- 결과: pass / fail / blocked / not_run
- 근거: CI 링크 또는 비밀값을 제거한 보고서
- 실제 AWS / 집 PC 검증 여부:

## 영향과 복구
- 공통 계약·다른 파트 영향:
- 데이터·배포 영향:
- 실패 시 복구 방법:

## 검토 요청
- 중점적으로 확인할 사항:
```

인프라 변경의 기본 검증은 다음과 같다. 다른 영역은 Issue에 해당 빌드·단위·통합 검사 명령을 지정한다.

| 변경 범위 | 검증 |
|---|---|
| 공통 계약·인프라 문서 | `make prep-check prep-test` |
| Foundation | 공통 검사 + `make foundation-unit foundation-integration` |
| Runtime | 공통 검사 + `make runtime-unit runtime-integration` |
| 인프라 통합 | 같은 checkout에서 `make predeploy-check` |
| Lifecycle | `make lifecycle-unit lifecycle-integration` 및 공통 연계 `make integration-lifecycle` |

명령이 문서에 있다는 이유만으로 구현·통과를 가정하지 않는다. 필요한 코드·도구가 없거나 필수 검사가 실패하면 그 상태를 기록한다. 실제 AWS·GPU 검증을 모의 시험으로 대체해 성공 처리하지 않는다.

최종 Merge 조건:

- Issue 완료 기준과 적용 대상 검증을 충족한다.
- 최소 1명의 다른 팀원이 리뷰한다.
- 수정 요청과 충돌을 해결한다.
- 통합 담당자가 PR 대상·변경 범위·최종 검사 결과를 확인한다.

자동 리뷰는 팀원 리뷰의 보조 근거다. 리뷰 이후 코드가 바뀌면 영향받는 검사를 다시 실행한다. 각 파트 PR을 통합한 뒤 통합 checkout에서도 필요한 검증을 수행한다.

## 6. 역할과 책임

| 역할 | 책임 |
|---|---|
| Issue 담당자 | 개발·검증·커밋·PR·완료 기준과 미검증 항목 기록 |
| PR 리뷰어 | 동작·변경 범위·데이터·보안 영향 확인, 승인 또는 수정 요청 |
| 통합 담당자 | 승인된 PR 병합·충돌 처리·통합 검증 |
| 릴리스 담당자 | main 반영 범위 확인·릴리스 PR·Issue 종료 관리 |

개발 도구나 보조 도구를 사용하더라도 최종 검증과 PR 책임은 담당 팀원에게 있다.

## 7. Issue 종료 기준

**main 릴리스 완료 기준으로 통일한다.**

`feature/infra` 또는 `develop` 병합 후에는 `통합 완료·릴리스 대기`로 관리하고 PR에 `Related #번호`를 남긴다. 필요하면 GitHub Development 영역에서 수동 연결한다.

릴리스 담당자는 완료 기준을 충족한 Issue만 `develop → main` PR에 기록한다.

```text
Closes #42
Closes #43
```

GitHub의 종료 키워드는 기본 브랜치 대상 PR에서 동작한다. 저장소의 기본 브랜치를 main으로 설정한 뒤 사용한다. [GitHub 공식 연결·종료 규칙](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)

실제 배포·통신·GPU 검증이 완료 기준인 Issue는 로컬 시험이나 코드 병합만으로 종료하지 않는다. 실환경 검증을 마친 후 종료 대상으로 포함한다. 상위 Issue는 하위 작업과 자체 통합 완료 기준까지 확인한다.

중복·취소·작업 철회로 닫을 때는 완료와 구분해 사유를 남긴다.

## 8. 승인과 보존 규칙

- 승인된 작업 범위의 읽기·로컬 검증은 매번 재확인하지 않고 진행한다.
- 담당 Issue의 범위를 벗어난 파일 변경은 먼저 조율한다.
- main·develop·feature/infra 통합 브랜치에는 작업 코드를 직접 push하지 않고 PR로 통합한다.
- 공유 브랜치 force push와 다른 작업 경로의 reset·clean·rebase를 임의로 수행하지 않는다.
- AWS 생성·수정·삭제, 과금 작업, 실제 배포는 별도 승인 후 진행한다.
- 집 PC·Tailscale·원격 호스트 설정 변경도 승인 범위를 확인한다.
- Issue 할당·PR 승인·코드 Merge를 실제 AWS 실행 허가로 간주하지 않는다.
- 일반 push만으로 Terraform apply·destroy·원격 배포가 실행되지 않도록 분리한다.
- 비밀번호·API 키·개인키·실제 inventory·Terraform state/plan·민감 로그를 Git이나 공개 Issue에 게시하지 않는다.
- 삭제·포맷·전역 volume prune·checkpoint 초기화로 실패를 해결하지 않는다.

로컬 시험 자원의 생성·정리 범위는 작업에 포함해 명시하고 해당 작업이 만든 자원만 정리한다. 실패·중단·부분 성공·남은 자원은 기록하고 인계한다.

## 9. 도입 순서

1. GitHub 원격 저장소를 연결하고 main·develop·인프라 통합 기준을 확정한다.
2. 협업 가이드·Issue/PR 템플릿·리뷰 정책을 공통 작업으로 등록한다.
3. 기존 미커밋 작업과 검증 이력을 각각 Issue에 연결한다.
4. 검토된 작업을 Issue 번호 포함 커밋과 PR로 제출한다.
5. 공통 기준을 먼저 반영하고 의존 작업을 정해진 PR 대상으로 통합한다.
6. 통합 검증 후 develop → main 릴리스를 진행한다.

Issue 템플릿은 `.github/ISSUE_TEMPLATE/`, PR 템플릿은 `.github/PULL_REQUEST_TEMPLATE.md`에 준비한다. 실제 파일 추가 시 저장소의 공통 파일 소유권에도 등록한다. [GitHub 템플릿 문서](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository)

브랜치 보호는 PR·다른 팀원의 승인·필수 CI 검사를 요구하고 강제 push를 제한하도록 설정한다. 실제 설정 여부·권한·사용 가능한 기능을 확인하며 이 문서만으로 적용됐다고 표시하지 않는다.

2026-11-04 기준 정제된 새 Private 저장소를 별도로 준비했다. `main`은 릴리스, `develop`은 전체 개발 통합, `feature/infra`는 인프라 통합 브랜치로 유지한다. 과거 Git 커밋 SHA와 PR 번호를 새 저장소에 강제로 복제하지 않고 안전한 작업 단계별 커밋을 재구성한다. 실제 AWS 배포 승인은 GitHub 코드 병합과 별도이며, 향후 Public 전환 전 모든 Issue·PR·Actions 로그를 다시 점검한다.

**Private 무료 플랜의 브랜치 보호·Ruleset 기능은 실제 계정 제공 범위에 따라 비활성일 수 있다.** 이 경우 팀원 리뷰·PR 기반 Merge 정책을 절차로 우선 적용하며 강제 규칙이 설정됐다고 주장하지 않는다.

## 최종 체크리스트

- [ ] Issue·담당자·수정 범위·완료 기준이 정해져 있다.
- [ ] 올바른 기준과 작업 브랜치에서 개발했다.
- [ ] Commit·PR에 실제 Issue 번호가 있다.
- [ ] 해당 범위의 검증과 미검증 항목을 기록했다.
- [ ] 다른 팀원의 리뷰와 수정 요청 처리가 끝났다.
- [ ] 올바른 PR 대상으로 병합한다.
- [ ] 코드 통합과 실제 배포 승인을 구분했다.
- [ ] main 릴리스와 완료 기준에 맞춰 Issue를 종료한다.

**팀 공통 원칙: Issue로 범위를 정하고, 검증으로 근거를 남기며, 리뷰된 PR로 통합한다.**

## 10. 현재 GitHub 운영 상태와 로컬 보호 장치 (2026-11-04)

- 현재 Private 저장소에서 사용하는 장기 공유 브랜치는 `main`, `develop`, `feature/infra`이다. `feature/infra`는 최신 `develop` 기준으로 다시 생성했으며, 과거의 독립 이력을 직접 되살린 브랜치가 아니다.
- 일반 기능은 `feat/<Issue번호>-설명`, 인프라 작업은 `feat/<Issue번호>-infra-설명`처럼 **Issue별 단기 브랜치**로 개발하고 PR을 생성한다. 인프라 작업은 `feature/infra`로, 전체 통합은 `develop`으로, 릴리스는 `main`으로 올린다.
- 현재 GitHub Private 플랜에서는 **브랜치 보호 API와 Ruleset API가 HTTP 403**을 반환한다. 따라서 필수 리뷰·필수 상태검사를 GitHub 서버가 강제한다고 볼 수 없다. **팀 합의에 의한 PR 절차 + CI 검증 + 각 Clone의 로컬 Hook**으로 보완한다. Public 전환 직전에 플랜·권한을 재확인하고 서버 측 보호를 설정해야 한다.
- `feature/infra`는 장기 브랜치이므로 Repository의 **Merge 후 브랜치 자동 삭제 설정은 사용하지 않는다.** PR 병합 후 단기 기능 브랜치는 수동으로 삭제한다.
- 팀원별 Clone에서 `bash scripts/setup-git-hooks.sh`를 **한 번 실행**한다. 이 명령은 해당 Clone에 `core.hooksPath=scripts/hooks`를 설정한다. Git 설정은 이 저장소에만 적용되고 다른 Git 저장소는 변경하지 않는다.
- 설치된 `pre-commit`은 실제 스테이징된 파일의 비밀정보·개인정보·내부 자료를 검사한다. `pre-push`는 `main`, `develop`, `feature/infra`에 대한 직접 Push를 막고 PR로 작업하도록 안내한다. GitHub 웹·다른 Clone의 Push까지 막는 서버 보호는 아니므로 이를 보안 보장으로 간주하지 않는다.
- 장애 시 긴급 복구 목적에 한해 저장소 소유자가 `MLO_ALLOW_SHARED_BRANCH_PUSH=1`을 명시할 수 있지만, 사전 백업·리뷰·기록이 필요하다. 평소 개발에서는 사용하지 않는다.
- 정적 검사 보고서, 계정별 권한 점검 결과, 개인 기기/접속 로그, 개인정보 탐지 원문은 Git 제외 경로에서 보관하고 PR에는 **비밀값 없는 요약·검증 명령·결과**만 넣는다.
- `docs/`는 공개 가능한 설계·계약·개발 가이드만 추적한다. 미래 Public 전환 전에 과거 커밋 이력, Issue/PR/Actions 로그, 모든 이미지의 화면 텍스트 및 라이선스, GitHub Repository visibility를 별도로 최종 감사한다.

설치·검증 예시:

```bash
bash scripts/setup-git-hooks.sh
git config --local --get core.hooksPath  # scripts/hooks
python3 scripts/check_public_release.py
git switch develop
git pull --ff-only origin develop
git switch -c feat/11-example
# 변경 → git add → git commit -m "chore(git): 예시 변경 (#11)"
# git push -u origin feat/11-example
# GitHub PR: feat/11-example → develop
```

**운영 적용 원칙:** PR의 다른 팀원 리뷰가 확보되지 않았다면 자동화 검사 성공만으로 리뷰 완료라고 간주하지 않는다. 실제 GitHub 설정·승인 현황을 확인한 뒤 통합한다.
