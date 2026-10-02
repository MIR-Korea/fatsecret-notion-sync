import os
import math
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from requests_oauthlib import OAuth1

FS_URL = "https://platform.fatsecret.com/rest/food-entries/v2"
NOTION_URL = "https://api.notion.com/v1"
LOCAL_TIMEZONE = ZoneInfo("Asia/Seoul")
RAW_DB_ID = None
SUMMARY_DB_ID = "cb92ef8f87e248d9a405c78f0e9dd306"
CALORIE_TARGET = 2300.0
PROTEIN_TARGET = 150.0
PERSONAL_OS_INGEST_URL = "https://gobpgixochawblszmqob.supabase.co/functions/v1/personal-os-nutrition-ingest"


def secret(name):
    raw = os.environ.get(name, "")
    value = raw.strip()
    if raw != value:
        print(f"{name}: 앞뒤 공백을 제거했습니다.")
    if not value or any(ch.isspace() for ch in value) or ":" in value:
        print(f"{name}: 값 형식이 올바르지 않습니다.")
        raise ValueError(f"{name} 형식 오류")
    return value


oauth = OAuth1(
    secret("FATSECRET_CONSUMER_KEY"),
    client_secret=secret("FATSECRET_CONSUMER_SECRET"),
    resource_owner_key=secret("FATSECRET_ACCESS_TOKEN"),
    resource_owner_secret=secret("FATSECRET_ACCESS_TOKEN_SECRET"),
    signature_method="HMAC-SHA1",
)
headers = {}


def configure_notion():
    global RAW_DB_ID, headers
    RAW_DB_ID = secret("NOTION_DATABASE_ID")
    headers = {"Authorization": f"Bearer {secret('NOTION_TOKEN')}",
               "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
    expected = {"Name": "title", "Date": "date", "Meal": "select",
                "Food": "rich_text", "FatSecretID": "rich_text",
                "Calories": "number", "Carbs": "number", "Protein": "number", "Fat": "number"}
    r = requests.get(f"{NOTION_URL}/databases/{RAW_DB_ID}", headers=headers, timeout=30)
    r.raise_for_status()
    properties = r.json().get("properties", {})
    if any(properties.get(name, {}).get("type") != kind for name, kind in expected.items()):
        raise RuntimeError("Notion 음식 DB 스키마가 일치하지 않습니다.")


def normalize_entries(entries):
    """Reject invalid snapshots before either sink can delete anything."""
    unique = {}
    for entry in entries:
        entry_id = str(entry.get("food_entry_id", "")).strip()
        if not entry_id or len(entry_id) > 160:
            raise ValueError("invalid_food_entry_id")
        item = {"food_entry_id": entry_id,
                "food_entry_name": str(entry.get("food_entry_name", "이름 없음")),
                "meal": str(entry.get("meal", ""))}
        if len(item["food_entry_name"]) > 2000:
            raise ValueError("food_name_too_long")
        for key, ceiling in [("calories", 10000), ("carbohydrate", 2000), ("protein", 2000), ("fat", 2000)]:
            value = float(entry.get(key) or 0)
            if not math.isfinite(value) or not 0 <= value <= ceiling:
                raise ValueError("invalid_nutrition_value")
            item[key] = round(value, 2)
        if entry_id in unique and unique[entry_id] != item:
            raise ValueError("conflicting_food_entry_id")
        unique[entry_id] = item
    if len(unique) > 200:
        raise ValueError("snapshot_too_large")
    return list(unique.values())


def food_records(entries, day):
    return [{"fatsecret_id": e["food_entry_id"], "date": day.isoformat(),
             "food": e["food_entry_name"],
             "meal": {"Breakfast": "아침", "Lunch": "점심", "Dinner": "저녁"}.get(e["meal"], "간식"),
             "calories": e["calories"], "carbs": e["carbohydrate"],
             "protein": e["protein"], "fat": e["fat"]} for e in entries]


def entries_for(day):
    r = requests.get(
        FS_URL,
        params={"date": (day - date(1970, 1, 1)).days, "format": "json"},
        auth=oauth,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    if data.get("error"):
        code = data["error"].get("code", "unknown")
        print("FatSecret API 오류 코드:", code)
        raise RuntimeError("FatSecret API 오류")

    # 기록이 없는 날짜에는 food_entries가 생략되거나 null일 수 있습니다.
    container = data.get("food_entries")
    if container is None:
        return []
    if not isinstance(container, dict):
        raise RuntimeError("FatSecret food_entries 응답 형식이 올바르지 않습니다.")

    value = container.get("food_entry")
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    raise RuntimeError("FatSecret food_entry 응답 형식이 올바르지 않습니다.")


def notion_pages_for(day, database_id, date_property, entry_id=None):
    pages, cursor = [], None
    while True:
        body = {
            "page_size": 100,
            "filter": {
                "property": date_property,
                "date": {"equals": day.isoformat()},
            },
        }
        if cursor:
            body["start_cursor"] = cursor
        if entry_id is not None:
            body["filter"] = {"property": "FatSecretID", "rich_text": {"equals": entry_id}}
        r = requests.post(
            f"{NOTION_URL}/databases/{database_id}/query",
            headers=headers,
            json=body,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            return pages
        cursor = data["next_cursor"]


def fatsecret_id(page):
    values = (
        page.get("properties", {})
        .get("FatSecretID", {})
        .get("rich_text", [])
    )
    return "".join(value.get("plain_text", value.get("text", {}).get("content", "")) for value in values)


def raw_properties_for(entry, day):
    name = str(entry.get("food_entry_name", "이름 없음"))
    meal = {
        "Breakfast": "아침",
        "Lunch": "점심",
        "Dinner": "저녁",
    }.get(str(entry.get("meal", "")), "간식")
    return {
        "Name": {"title": [{"text": {"content": name}}]},
        "Meal": {"select": {"name": meal}},
        "Date": {"date": {"start": day.isoformat()}},
        "Calories": {"number": float(entry.get("calories") or 0)},
        "Carbs": {"number": float(entry.get("carbohydrate") or 0)},
        "Protein": {"number": float(entry.get("protein") or 0)},
        "Fat": {"number": float(entry.get("fat") or 0)},
        "Food": {"rich_text": [{"text": {"content": name}}]},
        "FatSecretID": {
            "rich_text": [
                {"text": {"content": str(entry["food_entry_id"])}}
            ]
        },
    }


def summary_properties_for(entries, day):
    def total(key):
        return round(sum(float(entry.get(key) or 0) for entry in entries), 2)

    calories = total("calories")
    protein = total("protein")
    return {
        "날짜": {"title": [{"text": {"content": day.isoformat()}}]},
        "식단 날짜": {"date": {"start": day.isoformat()}},
        "칼로리": {"number": calories},
        "탄수화물": {"number": total("carbohydrate")},
        "단백질": {"number": protein},
        "지방": {"number": total("fat")},
        "칼로리 목표": {"number": CALORIE_TARGET},
        "단백질 목표": {"number": PROTEIN_TARGET},
        "남은 칼로리": {"number": round(CALORIE_TARGET - calories, 2)},
        "남은 단백질": {"number": round(PROTEIN_TARGET - protein, 2)},
        "음식 수": {"number": len(entries)},
    }


def nutrition_totals(entries, day):
    def total(key):
        return round(sum(float(entry.get(key) or 0) for entry in entries), 2)

    return {
        "date": day.isoformat(),
        "calories": total("calories"),
        "carbs": total("carbohydrate"),
        "protein": total("protein"),
        "fat": total("fat"),
        "food_count": len(entries),
    }


def github_oidc_token():
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_token:
        raise RuntimeError("GitHub Actions OIDC 환경이 없습니다.")

    separator = "&" if "?" in request_url else "?"
    response = requests.get(
        f"{request_url}{separator}audience=personal-os-supabase",
        headers={"Authorization": f"Bearer {request_token}"},
        timeout=30,
    )
    response.raise_for_status()
    token = response.json().get("value")
    if not token:
        raise RuntimeError("GitHub OIDC 토큰을 받지 못했습니다.")
    return token


def sync_personal_os(days, entries=None):
    response = requests.post(
        PERSONAL_OS_INGEST_URL,
        headers={
            "Authorization": f"Bearer {github_oidc_token()}",
            "Content-Type": "application/json",
        },
        json={"days": days} if entries is None else {"snapshot": True, "days": days, "entries": entries},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Personal OS 동기화 응답이 올바르지 않습니다.")
    print("Personal OS 동기화:", result.get("upserted", 0), "일",
          "음식", result.get("upserted_entries", 0), "삭제", result.get("deleted_entries", 0))
    return result


def write_page(method, path, body):
    r = requests.request(
        method,
        f"{NOTION_URL}{path}",
        headers=headers,
        json=body,
        timeout=30,
    )
    r.raise_for_status()


def upsert_summary(day, entries):
    pages = notion_pages_for(day, SUMMARY_DB_ID, "식단 날짜")
    properties = summary_properties_for(entries, day)
    if pages:
        write_page("PATCH", f"/pages/{pages[0]['id']}", {"properties": properties})
        # 날짜당 1행을 보장합니다. 기존 중복이 생겼다면 첫 행만 남깁니다.
        for duplicate in pages[1:]:
            write_page("PATCH", f"/pages/{duplicate['id']}", {"archived": True})
            time.sleep(0.4)
        return "수정"

    write_page(
        "POST",
        "/pages",
        {
            "parent": {"database_id": SUMMARY_DB_ID},
            "properties": properties,
        },
    )
    return "추가"


def sync_day(day, entries=None, protected_ids=None):
    entries = normalize_entries(entries_for(day)) if entries is None else entries
    protected_ids = protected_ids or set()
    pages = notion_pages_for(day, RAW_DB_ID, "Date")

    # FatSecretID를 고유 키로 사용합니다. 과거 중복 행이 있다면
    # 첫 번째 행만 유지하고 나머지는 휴지통으로 이동합니다.
    existing = {}
    duplicate_pages = []
    for page in pages:
        entry_id = fatsecret_id(page)
        if not entry_id:
            continue
        if entry_id in existing:
            duplicate_pages.append(page)
        else:
            existing[entry_id] = page

    added = updated = archived = 0
    for duplicate in duplicate_pages:
        write_page("PATCH", f"/pages/{duplicate['id']}", {"archived": True})
        archived += 1
        time.sleep(0.4)

    current_ids = {str(entry["food_entry_id"]) for entry in entries}

    for entry in entries:
        entry_id = str(entry["food_entry_id"])
        # Look beyond this date: an edited/moved FatSecret record keeps its ID.
        if entry_id not in existing:
            matches = notion_pages_for(day, RAW_DB_ID, "Date", entry_id=entry_id)
            if matches:
                existing[entry_id] = matches[0]
                for duplicate in matches[1:]:
                    write_page("PATCH", f"/pages/{duplicate['id']}", {"archived": True})
                    archived += 1
                    time.sleep(0.4)
        if entry_id in existing:
            write_page(
                "PATCH",
                f"/pages/{existing[entry_id]['id']}",
                {"properties": raw_properties_for(entry, day)},
            )
            updated += 1
        else:
            write_page(
                "POST",
                "/pages",
                {
                    "parent": {"database_id": RAW_DB_ID},
                    "properties": raw_properties_for(entry, day),
                },
            )
            added += 1
        time.sleep(0.4)

    # API가 정상 응답했고 기존 Notion 행이 FatSecret에서 사라졌다면
    # 해당 행을 휴지통으로 이동합니다. 전체 음식 삭제도 반영됩니다.
    for entry_id, page in existing.items():
        if entry_id not in current_ids and entry_id not in protected_ids:
            write_page("PATCH", f"/pages/{page['id']}", {"archived": True})
            archived += 1
            time.sleep(0.4)

    # 음식이 있거나 기존 기록이 삭제된 날만 요약을 갱신합니다.
    # 전부 삭제된 날은 합계를 0으로 만들어 기존 요약과 일치시킵니다.
    if entries or existing:
        summary_action = upsert_summary(day, entries)
        print("일일 요약", summary_action + ":", day)
    else:
        print("기록 없음 - 변경 없음:", day)

    return added, updated, archived, nutrition_totals(entries, day)


def main():
    totals = [0, 0, 0]
    failures = []
    snapshots = []
    today = datetime.now(LOCAL_TIMEZONE).date()
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        try:
            snapshots.append((day, normalize_entries(entries_for(day))))
        except Exception as exc:
            failures.append(("FatSecret", day, type(exc).__name__))
        time.sleep(0.3)

    # Supabase is the first independent output. A Notion outage cannot block it.
    for day, entries in snapshots:
        try:
            sync_personal_os([nutrition_totals(entries, day)], food_records(entries, day))
        except Exception as exc:
            failures.append(("Supabase", day, type(exc).__name__))

    try:
        configure_notion()
    except Exception as exc:
        failures.append(("Notion 설정", None, type(exc).__name__))
    else:
        protected_ids = {e["food_entry_id"] for _, entries in snapshots for e in entries}
        for day, entries in snapshots:
            try:
                added, updated, archived, _ = sync_day(day, entries, protected_ids)
                totals = [a + b for a, b in zip(totals, (added, updated, archived))]
            except Exception as exc:
                failures.append(("Notion", day, type(exc).__name__))
    if failures:
        for service, day, kind in failures:
            print(f"재시도 필요: 서비스={service} 날짜={day} 종류={kind}")
        print("인증값·응답 본문은 출력하지 않습니다. 다음 실행에서 재시도합니다.")
        raise SystemExit(1)

    print(
        f"완료 - 추가: {totals[0]}, 수정: {totals[1]}, "
        f"휴지통 이동: {totals[2]}"
    )


if __name__ == "__main__":
    main()
