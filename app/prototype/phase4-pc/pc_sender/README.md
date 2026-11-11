# 추론용 PC 곡 신청 작업자

이 폴더에서 Python 3.12로 `python -B -m pc_sender`를 실행합니다. `run`은 서버가 승인한 작업을 조회하고, 준비된 음원이 있는 곡 한 건을 점유해 MERT v2 추론 결과를 서버에 보냅니다. `--poll-seconds`를 지정하면 이 동작을 반복합니다. HTTP·SQLite 송신 모듈은 표준 라이브러리만 사용합니다. MERT 실행에는 기존 실험의 Linux 컨테이너 환경과 모델 파일이 필요합니다.

## 4차 서버 설정

- 4차 `app` 디렉터리에서 기존 Django 환경으로 `python -m backend.manage migrate`를 실행하면 `phase4_ops`의 신청·작업·수신 테이블이 준비됩니다. DB는 해당 환경의 Django 설정을 따릅니다.
- `/platform/business`에서 활성 매장의 점주가 한 번에 최대 20곡을 신청합니다. `/admin/music`의 음악 관리자는 영상이 곡과 일치하는지 확인하고 URL을 승인합니다. 승인하면 PC가 조회할 작업이 `READY`가 됩니다.
- API 프로세스의 `PHASE4_WORKER_TOKEN`을 24자 이상의 임의 토큰으로 설정하고 추론용 PC에 같은 값을 비밀 환경 변수로 제공합니다. 운영 연결은 HTTPS를 사용하며, Django가 요청을 HTTPS로 인식해야 합니다. 기본 작업 임대는 900초이며 `PHASE4_LEASE_SECONDS`로 설정합니다.
- DB에 저장된 태그 후보는 `/admin/music`의 **추론용 PC 처리 현황**에서 확인합니다. 수신 결과를 기존 추천 카탈로그에 반영하는 작업은 기존 음악 검수·발행 절차에서 진행합니다.

## 입력과 실행

- `--wav-root`에 사용 권한을 확보한 24 kHz 모노 WAV를 `<YouTube 영상 ID>.wav`로 둡니다. YouTube 주소는 영상 ID와 원본 참조로 사용합니다. 기본 실행에는 URL에서 음원을 받는 동작이 없습니다.
- `--baseline-root`는 기존 `20261103-mert2-maest-v1`의 루트입니다. `code/`, `prepared/plan-frozen.json`, `models/mert2/`, `runs/`를 읽을 수 있어야 합니다.
- `--refinement-root`는 기존 `20261104-mert2-refinement-v1`의 루트입니다. `code/`, `selection-frozen.json`, `ensemble/`, `runs/`를 읽을 수 있어야 합니다. 기존 실험 파일은 읽기 전용으로 연결할 수 있습니다. 모델·실험 코드 사본을 서버에서 내려받지 않습니다.
- 서버 주소는 `/api/phase4`까지 지정합니다. 서버의 `PHASE4_WORKER_TOKEN`과 같은 값을 추론용 PC의 `PHASE4_SENDER_TOKEN` 환경 변수로 전달합니다. 로컬 테스트의 숫자 루프백 HTTP는 양쪽에서 명시적으로 허용한 경우에만 사용합니다.

기존 Linux 실행 환경에 파일을 연결한 뒤 아래 명령에서 경로를 맞춥니다.

```bash
python -B -m pc_sender.mert --baseline-root /baseline --refinement-root /work --check
python -B -m pc_sender.mert --baseline-root /baseline --refinement-root /work --wav /prepared-wav/AAAAAAAAAAA.wav
python -B -m pc_sender --outbox /pc-state/outbox.sqlite3 run --base-url https://receiver.example/api/phase4 --wav-root /prepared-wav --baseline-root /baseline --refinement-root /work
```

`--check`는 모델·코드 경로, 선택 파일과 앙상블 manifest 해시, 사전과 모듈 가용성을 확인합니다. CUDA 실행과 실제 가중치 검증은 `--wav`가 모델을 로드할 때 확인합니다. `--device cpu`는 명시적인 CPU 실행 옵션입니다. 기본값은 `cuda`입니다. 작업자는 임대가 끝나기 전에 송신할 여유를 남기고 최대 추론 시간을 결정합니다. 실패한 추론은 후보를 만들지 않고 서버 임대는 자연 만료됩니다.

음원이 없는 작업은 다른 곡을 처리할 수 있도록 건너뜁니다. 운영자가 허용된 음원 준비 프로그램을 별도로 보유한 경우에는 `--audio-preparer /absolute/path/to/program`과 반복 가능한 `--audio-preparer-arg`를 전달할 수 있습니다. 프로그램은 `--youtube-url <canonical URL>`과 `--wav-output <temporary WAV path>`를 받습니다. 셸 없이 실행하며 작업자가 실행 파일과 인자를 정합니다. 프로그램은 지정한 임시 파일을 완성한 뒤 종료 코드 0으로 끝나야 합니다. 기존 WAV를 우선 사용하며 준비 프로그램에는 수신 토큰을 전달하지 않습니다.

## 후보와 수신 상태

- 동결된 MERT v2 L24 전체곡 평균 특징과 3차 50:50 MLP·RBF-SVM 앙상블을 재사용합니다. 실험에서 고정한 대표 출력 기준으로 장르 최대 1개, 분위기 최대 2개를 후보로 보냅니다. 기준을 통과한 후보가 없으면 빈 배열을 보냅니다. 원본 14개 장르와 9개 분위기 코드를 그대로 유지합니다.
- `CANDIDATES_GENERATED`는 추론과 후보 저장 결과입니다. `RECEIVED`는 같은 결과 ID·본문 해시로 서버 DB에 검수 대기 후보가 저장되었음을 확인한 상태입니다. 최종 검수와 추천 카탈로그 발행은 별도 단계입니다.
- 응답 유실·통신 장애는 같은 결과 ID와 본문으로 다시 보냅니다. `status`·`history <result_id>`로 확인하고, 추론을 건너뛰어 전송만 다시 하려면 `send --base-url <주소>`를 사용합니다. 외부에서 별도로 만든 계약상 후보 JSON을 등록하는 `import <파일>`도 지원합니다.
- SQLite outbox와 WAV·모델은 추론용 PC에서 접근 권한을 관리합니다. outbox에는 재전송용 후보와 작업 임대 값이 들어갑니다.

## 확인 범위

`python -B -m unittest discover -s tests`는 합성 WAV와 가짜 서버/모델로 준비 실패 건너뛰기, 실행 인자, 모델 실패, ACK 및 멱등 재시도를 확인합니다. 실제 GPU·추론용 PC의 저장 파일·운영 서버 연결은 해당 환경에서 별도로 확인해야 합니다.
