# TASK-02 레시피·보관 가이드·시드 데이터 작성

- 단계: M1 (TASK-01과 병렬 가능)
- 예상 시간: 1.5h (추정, 내용 작성 포함)
- 선행 작업: 없음
- 관련 개념: #3 RAG, #12 평가 (정답 기준 데이터)
- 관련 리뷰 결정: 독립 검토 8번 (레시피 형식이 기계 파싱 가능해야 함)

## 목표
RAG 검색 대상 문서와, 결정적 도구가 읽는 구조화 데이터를 만든다. 사용자의 Assignment(실제 냉장고 재료 10개, 가족 선호)를 시드에 반영한다.

## 작업 내용
- [ ] 레시피 문서 3개, 카테고리별 8~10개 (총 24~30개)
  - `data/recipes_korean.md`, `data/recipes_japanese.md`, `data/recipes_italian.md`
  - 레시피마다 고정 형식: `## 요리명`, 메타 표(카테고리, 주재료, 1인분 kcal), 부재료 표(재료, 인분당 양, 기준 단위), 조리 순서
  - 주재료 한우(소고기)를 쓰는 요리가 카테고리마다 최소 1개 (대표 시나리오: 소고기 무국, 규동, 소고기 라구 파스타)
  - 갑각류가 들어간 요리 최소 1개 (E03 알레르기 검증용)
- [ ] `data/storage_guide.md`
  - 표 열: 품목, 별칭, 기준 단위, 권장 보관일수, 개당 평균 무게 g
  - 예: 소고기(별칭 한우, 우둔, 차돌), 두부(1개 = 300g)
  - 망고는 넣지 않는다 (N02 검증용)
  - 표 아래에 품목별 보관 팁 문단 (RAG 검색용)
- [ ] `data/seed_inventory.json`: 날짜는 `input_offset_days`, `expiry_offset_days` 상대값. 한우 600g은 `expiry_offset_days: 2`. S/N `2026-11-001` 포함 (E01 중복 검증용)
- [ ] `data/seed_users.json`: dad·mom·jimin, 초기 평점(dad: 소고기 무국 4.5, 규동 4.0), 선호·비선호, jimin 갑각류 알레르기

## 대상 파일
- `data/recipes_*.md`, `data/storage_guide.md`, `data/seed_inventory.json`, `data/seed_users.json`

## 완료 기준
- 레시피 파서(TASK-04, TASK-05)가 모든 레시피의 주재료·부재료·kcal을 오류 없이 읽는다.
- 평가 12문항의 공통 전제(SERVICE.md 10.1)를 시드가 모두 만족한다.

## 참고
- SERVICE.md 9장 데이터, 10.1 공통 전제, 19장 Assignment
