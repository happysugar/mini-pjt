# TASK-03 DB 스키마와 시드 로더

- 단계: M1
- 예상 시간: 0.5h (추정)
- 선행 작업: TASK-01, TASK-02
- 관련 개념: #4 도구 다중 (DB)
- 관련 리뷰 결정: 리뷰 작업 T3 (체크포인트 DB 분리)

## 목표
도메인 데이터를 담는 `data/fridge.db`를 만들고, 시드를 `today()` 기준 날짜로 다시 채울 수 있게 한다.

## 작업 내용
- [x] `today()` 함수: `FRIDGE_TODAY` 환경변수가 있으면 그 날짜, 없으면 실제 오늘
- [x] SQLite 테이블 생성 (SERVICE.md 9장)
  - `inventory`, `deductions`, `ratings`, `meals`, `users`, `threads`, `item_units`
- [x] 시드 로더: 상대 일수를 `today()` 기준 날짜로 바꿔 삽입. DB를 지우고 새로 만드는 `reset_db()` 제공 (평가 문항 격리에 사용)
- [x] 체크포인트·장기 메모리는 별도 파일 `data/checkpoints.db` (이 task에서는 경로 상수만 정의)

## 대상 파일
- `src/tools.py` (DB 접근·시드 함수), `data/fridge.db`(실행 시 생성)

## 완료 기준
- `FRIDGE_TODAY=2026-11-20`에서 시드를 넣으면 한우 유효기간이 2026-11-22가 된다.
- `reset_db()`를 두 번 호출해도 같은 상태가 된다.

## 참고
- SERVICE.md 5.1 오늘 날짜, 9장 스키마

## 결과 (2026-11-18)
- `src/tools.py`: `today()`, `offset_date()`, `connect()`, `create_schema()`, `load_seed()`, `reset_db(db_path=None, base=None)`, 경로 상수 `FRIDGE_DB`·`CHECKPOINT_DB`.
- `reset_db()`는 파일을 지우지 않고 한 연결 안에서 테이블을 DROP 후 다시 만든다. Windows에서 다른 연결이 파일을 잡고 있어도 실패하지 않게 하기 위해서다. `db_path`로 다른 파일을 지정할 수 있어 테스트에서 임시 DB를 쓸 수 있다.
- 시드 평점의 `created_at`은 `YYYY-MM-DDT12:00:00` 형식으로 저장한다.
- `.gitignore`에 `data/*.db` 추가 (실행 시 생기는 DB는 커밋하지 않음).
- 검증: `FRIDGE_TODAY=2026-11-20`에서 한우 유효기간 2026-11-22, 피클 `NULL`/`unknown`. 중간에 쓰기를 한 뒤 다시 `reset_db()`해도 7개 테이블 내용이 처음과 같다. `FRIDGE_TODAY=2026-12-01`이면 한우는 2026-12-03.
- 열린 점: `seed_users.json`의 선호·비선호·알레르기는 SQLite 스키마(9장)에 자리가 없어 넣지 않았다. 설계상 장기 메모리 Store(TASK-16)에 들어가므로, 그 전에 쓰는 TASK-05 알레르기 필터는 시드 JSON을 직접 읽거나 Store를 먼저 채워야 한다.
