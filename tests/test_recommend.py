"""요리 추천 결정적 로직 테스트 (TASK-05, SERVICE.md 5.2·5.5).

모든 테스트는 `FRIDGE_TODAY=2026-11-20`과 임시 DB의 시드 상태에서 시작한다.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tools  # noqa: E402
from tools import RecommendationItem, ImminentUse, rank_key  # noqa: E402

CRUSTACEAN_DISHES = {name for name, r in tools.parse_recipes().items() if "갑각류" in r.allergens}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """시드를 넣은 임시 도메인 DB 연결을 연다."""
    monkeypatch.setenv("FRIDGE_TODAY", "2026-11-20")
    db = tmp_path / "fridge.db"
    tools.reset_db(db)
    c = tools.connect(db)
    yield c
    c.close()


def _only_stock(conn, *items):
    """주어진 품목만 남기고 나머지 재고를 소진 처리한다."""
    marks = ",".join("?" * len(items))
    conn.execute(f"UPDATE inventory SET status = 'consumed' WHERE item NOT IN ({marks})", items)


def _rate(conn, user_id, dishes, score):
    """요리 목록에 같은 점수의 평가를 넣는다."""
    recipes = tools.parse_recipes()
    conn.executemany(
        "INSERT INTO ratings (user_id, dish, cuisine, score, created_at) VALUES (?, ?, ?, ?, ?)",
        [(user_id, d, recipes[d].cuisine, score, "2026-11-19T12:00:00") for d in dishes],
    )


def _item(dish, rating=None, count=0, imminent=None):
    """정렬 테스트용 추천 항목을 만든다."""
    return RecommendationItem(
        dish=dish,
        cuisine="한식",
        main_ingredient="소고기",
        rating=rating,
        rating_count=count,
        is_new=rating is None,
        imminent_ingredients=[ImminentUse(item="소고기", d_day=d) for d in (imminent or [])],
        kcal_per_serving=100,
        source="recipes_korean.md",
    )


# ── 레시피 파서 ─────────────────────────────────────────────────


def test_parse_all_recipes():
    recipes = tools.parse_recipes()
    assert len(recipes) == 36
    for cuisine in tools.CUISINES:
        assert sum(r.cuisine == cuisine for r in recipes.values()) == 9
    for r in recipes.values():
        assert r.kcal > 0 and r.steps and r.summary
        assert r.main in {i.item for i in r.ingredients}
        for ing in r.ingredients:
            assert tools.resolve_item(ing.item) == ing.item
            assert ing.unit == tools.guide_info(ing.item)["base_unit"]
    gyudon = recipes["규동"]
    assert gyudon.source == "recipes_japanese.md" and gyudon.kcal == 680


# ── 대표 시나리오 ─────────────────────────────────────────────


def test_hero_scenario(conn):
    result = tools.recommend(conn, "dad")
    picks = [(i.slot, i.dish, i.rating, i.is_new) for i in result.items]
    assert picks == [
        ("한식", "소고기 무국", 4.5, False),
        ("일식", "규동", 4.0, False),
        ("이탈리아식", "소고기 라구 파스타", None, True),
        ("중식", "소고기 굴소스볶음", None, True),
    ]
    assert result.notes == []
    ragu = result.items[2]
    assert ragu.missing_ingredients == ["토마토 캔"]
    assert [(u.item, u.d_day) for u in ragu.imminent_ingredients] == [("소고기", 2)]
    assert ragu.source == "recipes_italian.md"
    assert not result.empty_slots and not result.notes


def test_low_rating_never_fills_slot(conn):
    result = tools.recommend(conn, "dad", category="이탈리아식")
    assert "까르보나라" not in {i.dish for i in result.items}


def test_expired_main_is_not_candidate(conn):
    result = tools.recommend(conn, "guest", category="한식")
    assert "시금치나물" not in {i.dish for i in result.items}
    candidates, _ = tools._safe_candidates(conn, [], [], 1)
    assert "시금치나물" not in {c.recipe.name for c in candidates}


def test_missing_scales_with_servings(conn):
    result = tools.recommend(conn, "dad", category="한식", servings=10)
    soup = next(i for i in result.items if i.dish == "소고기 무국")
    assert "소고기" in soup.missing_ingredients  # 80g × 10 > 600g


# ── 안전 필터 ───────────────────────────────────────────────────


@pytest.mark.parametrize("category", [None, *tools.CUISINES])
def test_allergy_never_recommended(conn, category):
    # 지민이 갑각류 요리를 최고점으로 평가했어도 나오면 안 된다.
    _rate(conn, "jimin", CRUSTACEAN_DISHES, 5)
    conn.execute("INSERT INTO inventory (sn, item, qty, unit, expiry_date, expiry_source, input_date)"
                 " VALUES ('2026-11-099', '새우', 500, 'g', '2026-11-21', 'user', '2026-11-20')")
    result = tools.recommend(conn, "jimin", category=category)
    assert result.items
    assert not CRUSTACEAN_DISHES & {i.dish for i in result.items}


def test_allergy_by_ingredient_name(conn):
    result = tools.recommend(conn, "mom", allergies=["대하"], dislikes=[])
    assert "새우 로제 파스타" not in {i.dish for i in result.items}


def test_dislike_excluded(conn):
    result = tools.recommend(conn, "dad", dislikes=["소고기 무국"], allergies=[])
    assert "소고기 무국" not in {i.dish for i in result.items}
    assert result.items[0].dish == "김치볶음밥"


# ── 동점 정렬 ───────────────────────────────────────────────────


def test_rank_order():
    items = [
        _item("가", rating=5.0, count=9),                 # 임박 재료 없음
        _item("나", rating=3.0, count=1, imminent=[3]),
        _item("다", rating=3.0, count=1, imminent=[1]),
        _item("라", rating=4.0, count=1, imminent=[1]),
        _item("마", rating=4.0, count=2, imminent=[1]),
        _item("바", rating=4.0, count=2, imminent=[1]),
    ]
    ordered = [i.dish for i in sorted(items, key=rank_key)]
    # 임박 사용 → 최소 D-day → 평점 → 평가 횟수 → 이름
    assert ordered == ["마", "바", "라", "다", "나", "가"]


def test_rank_uses_min_d_day():
    assert rank_key(_item("가", imminent=[3, 0])) < rank_key(_item("나", imminent=[1]))


# ── 슬롯 규칙 ───────────────────────────────────────────────────


def test_extra_new_when_slots_have_none(conn):
    _rate(conn, "mom", ["소고기 굴소스볶음"], 4)
    result = tools.recommend(conn, "mom")
    slots = [(i.slot, i.dish, i.is_new) for i in result.items]
    assert slots[:4] == [
        ("한식", "소고기 무국", False),
        ("일식", "규동", False),
        ("이탈리아식", "새우 로제 파스타", False),
        ("중식", "소고기 굴소스볶음", False),
    ]
    # 슬롯에 신메뉴가 없으면 전체 신메뉴 중 앞선 2개를 추가한다
    assert slots[4:] == [("추가 신메뉴", "소고기 라구 파스타", True), ("추가 신메뉴", "감자 뇨끼", True)]


def test_only_one_new_menu_left(conn):
    _rate(conn, "dad", [d for d in tools.parse_recipes() if d not in ("소고기 무국", "규동", "까르보나라", "감자 뇨끼")], 4)
    result = tools.recommend(conn, "dad")
    assert [i.dish for i in result.items if i.is_new] == ["감자 뇨끼"]
    assert result.notes == ["새로 시도할 수 있는 요리가 1개뿐이에요"]


def test_new_slots_over_two_keep_two(conn):
    # 평가가 없는 guest는 네 슬롯이 모두 신메뉴라 정렬상 앞선 2개만 남긴다
    result = tools.recommend(conn, "guest")
    assert [(i.slot, i.dish) for i in result.items] == [("일식", "규동"), ("중식", "소고기 굴소스볶음")]
    assert [(e.slot, e.reason) for e in result.empty_slots] == [
        ("한식", "신메뉴는 한 번에 2개까지만 추천해요"),
        ("이탈리아식", "신메뉴는 한 번에 2개까지만 추천해요"),
    ]


def test_no_new_menu_left(conn):
    _rate(conn, "dad", [d for d in tools.parse_recipes() if d not in ("소고기 무국", "규동", "까르보나라")], 4)
    result = tools.recommend(conn, "dad")
    assert all(not i.is_new for i in result.items)
    assert len(result.items) == 4
    assert result.notes == ["새로 시도할 수 있는 요리가 없어요"]


def test_empty_slot_reasons(conn):
    _only_stock(conn, "두부")
    _rate(conn, "dad", ["두부조림"], 2)
    _rate(conn, "dad", ["된장찌개"], 2)
    result = tools.recommend(conn, "dad")
    assert [(i.slot, i.dish, i.is_new) for i in result.items] == [("일식", "두부 미소시루", True), ("중식", "마파두부", True)]
    assert [(e.slot, e.reason) for e in result.empty_slots] == [
        ("한식", "재고로 만들 수 있는 한식 요리 중 평점 3점 이상이거나 새로 시도할 요리가 없어요"),
        ("이탈리아식", "재고로 만들 수 있는 이탈리아식 요리가 없어요"),
    ]


def test_category_rated_two_plus_new(conn):
    result = tools.recommend(conn, "dad", category="한식")
    assert [(i.dish, i.is_new) for i in result.items] == [
        ("소고기 무국", False),
        ("김치볶음밥", False),
        ("감자조림", True),
        ("된장찌개", True),
    ]
    assert all(i.slot == "한식" for i in result.items)
    assert result.notes == []


def test_category_without_new(conn):
    _rate(conn, "dad", ["니쿠자가", "두부 미소시루", "오므라이스", "표고버섯 솥밥"], 4)
    result = tools.recommend(conn, "dad", category="일식")
    assert all(not i.is_new for i in result.items) and len(result.items) == 2
    assert result.notes == ["새로 시도할 수 있는 일식 요리가 없어요"]


def test_unsupported_category(conn):
    with pytest.raises(ValueError):
        tools.recommend(conn, "dad", category="멕시코식")


def test_result_is_pydantic(conn):
    data = tools.recommend(conn, "dad").model_dump()
    assert set(data) == {"user_id", "category", "items", "empty_slots", "notes"}
    assert tools.RecommendationResult.model_validate(data).items[0].dish == "소고기 무국"


# ── 가족 합의 규칙 ─────────────────────────────────────────────


def test_family_default(conn):
    result = tools.recommend_family(conn, "dad", ["mom", "jimin"])
    assert result.diners == ["dad", "mom", "jimin"]
    picks = [(i.slot, i.dish, i.rating, i.is_new) for i in result.items]
    # 소고기 무국 그룹 평점 = (4.5 + 4.0 + 3.0) / 3 = 3.83
    assert picks == [
        ("그룹 평점", "소고기 무국", 3.8, False),
        ("그룹 신메뉴", "소고기 굴소스볶음", None, True),
        ("그룹 신메뉴", "소고기 라구 파스타", None, True),
    ]
    assert result.within_calorie_budget is None


def test_family_excludes_any_diner_allergy(conn):
    _rate(conn, "dad", CRUSTACEAN_DISHES, 5)
    result = tools.recommend_family(conn, "dad", ["jimin"])
    assert not CRUSTACEAN_DISHES & {i.dish for i in result.items}


def test_family_calorie_limit(conn):
    result = tools.recommend_family(conn, "dad", ["mom"], calorie_budgets={"dad": 900, "mom": 300})
    assert result.within_calorie_budget is True
    assert all(i.kcal_per_serving <= 300 for i in result.items)


def test_family_calorie_fallback_lightest(conn):
    result = tools.recommend_family(conn, "dad", ["mom"], calorie_budgets={"dad": 50, "mom": 900})
    assert result.within_calorie_budget is False
    # 가장 가벼운 요리가 하나뿐이면 신메뉴도 1개만 나온다
    assert result.notes[0] == "모두의 남은 칼로리 안에 드는 요리가 없어 가장 가벼운 요리를 골랐어요"
    candidates, _ = tools._safe_candidates(conn, [], ["가지 파르미자나", "까르보나라"], 1)
    lightest = min(c.recipe.kcal for c in candidates)
    assert result.items and all(i.kcal_per_serving == lightest for i in result.items)


def test_family_nothing_found(conn):
    _only_stock(conn, "피클")
    result = tools.recommend_family(conn, "dad", ["mom", "jimin"])
    assert result.items == []
    assert result.notes[0].startswith("함께 먹을 수 있는 요리를 찾지 못했어요")
    assert "재고" in result.notes[0] and "알레르기" in result.notes[0]


def test_family_output_has_no_personal_values(conn):
    fields = set(tools.FamilyRecommendationResult.model_fields)
    assert fields == {"diners", "items", "empty_slots", "within_calorie_budget", "notes"}
