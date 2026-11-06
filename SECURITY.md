# Security and privacy reporting

이 저장소는 향후 공개될 수 있습니다. API 키, 이메일·전화번호, 실제 계정 ID,
개발 PC의 사용자 경로, 실제 운영 로그 및 보안 감사 보고서 원문을
Issue, Pull Request, Git 커밋에 올리지 마세요.

민감한 문제는 공개 Issue 대신 저장소 소유자의 승인된 비공개 연락 경로로
보고하세요. 유출 자격증명은 즉시 폐기·교체해야 합니다.

커밋 전: `python3 scripts/check_public_release.py --staged`
앱 검사: `python3 app/docker/check_git_candidates.py --staged`

이 검사는 휴리스틱 기반이며 이미지 픽셀, 특이한 인증자료,
Git 메타데이터·PR/Issue/Actions 기록에 대한 수동 검토가 별도로 필요합니다.

## Private 저장소의 현재 제약 및 공개 전환 기준

현재 Private 플랜에서는 GitHub 브랜치 보호·Ruleset 설정이 제한될 수 있으며,
GitHub Actions 검사 결과만으로 직접 Push·PR Merge가 서버에서 차단되는 것은 아닙니다.
팀원은 `bash scripts/setup-git-hooks.sh`로 로컬 보안 Hook을 설치하고,
공유 브랜치는 Issue가 연결된 PR을 통해서만 변경하세요.

Public으로 공개하기 전에는 다음 항목의 별도 검토가 필요합니다.

- 전체 브랜치·커밋 이력, Git 작성자 메타데이터, 숨은 Git 참조에 민감자료가 없는지 점검
- Issue·PR·Actions 로그·첨부파일과 과거 이미지 속 개인정보·식별자 확인
- 음악 데이터셋·스크린샷·이미지·외부 API 자료의 공개·재배포 권한 확인
- GitHub Secret Scanning, 서버 측 Branch Protection/Rulesets 및 팀 리뷰 승인 요구 설정
- 실제 AWS 계정·IP·비용·설정 파일 등 조직 내부 정보의 공개 범위 확인

**공개 전환은 보안검사 PASS만으로 자동 실행하지 않습니다.** 저장소 소유자의 최종 승인과 리뷰가 필요합니다.
