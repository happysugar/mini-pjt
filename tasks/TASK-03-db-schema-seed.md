# TASK-03 DB 스키마와 시드 로더

- 단계: M1
- 예상 시간: 0.5h (추정)
- 선행 작업: TASK-01, TASK-02
- 관련 개념: #4 도구 다중 (DB)
- 관련 리뷰 결정: 리뷰 작업 T3 (체크포인트 DB 분리)

## 목표
도메인 데이터를 담는 `data/fridge.db`를 만들고, 시드를 `today()` 기준 날짜로 다시 채울 수 있게 한다.

## 작업 내용
- [ ] `today()` 함수: `FRIDGE_TODAY` 환경변수가 있으면 그 날짜, 없으면 실제 오늘
- [ ] SQLite 테이블 생성 (SERVICE.md 9장)
  - `inventory`, `deductions`, `ratings`, `meals`, `users`, `threads`, `item_units`
- [ ] 시드 로더: 상대 일수를 `today()` 기준 날짜로 바꿔 삽입. DB를 지우고 새로 만드는 `reset_db()` 제공 (평가 문항 격리에 사용)
- [ ] 체크포인트·장기 메모리는 별도 파일 `data/checkpoints.db` (이 task에서는 경로 상수만 정의)

## 대상 파일
- `src/tools.py` (DB 접근·시드 함수), `data/fridge.db`(실행 시 생성)

## 완료 기준
- `FRIDGE_TODAY=2026-11-20`에서 시드를 넣으면 한우 유효기간이 2026-11-22가 된다.
- `reset_db()`를 두 번 호출해도 같은 상태가 된다.

## 참고
- SERVICE.md 5.1 오늘 날짜, 9장 스키마
