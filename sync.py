import os
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


def secret(name):
    raw = os.environ.get(name, "")
    value = raw.strip()
    if raw != value:
        print(f"{name}: 앞뒤 공백을 제거했습니다.")
    if not value or any(ch.isspace() for ch in value) or ":" in value:
        print(f"{name}: 값 형식이 올바르지 않습니다.")
        raise ValueError(f"{name} 형식 오류")
    return value


RAW_DB_ID = secret("NOTION_DATABASE_ID")
oauth = OAuth1(
    secret("FATSECRET_CONSUMER_KEY"),
    client_secret=secret("FATSECRET_CONSUMER_SECRET"),
    resource_owner_key=secret("FATSECRET_ACCESS_TOKEN"),
    resource_owner_secret=secret("FATSECRET_ACCESS_TOKEN_SECRET"),
    signature_method="HMAC-SHA1",
)
headers = {
    "Authorization": f"Bearer {secret('NOTION_TOKEN')}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


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


def notion_pages_for(day, database_id, date_property):
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
    return values[0].get("plain_text", "") if values else ""


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


def sync_day(day):
    entries = entries_for(day)

    # 빈 응답은 실제 미기록과 API 이상을 구분하기 어려우므로 삭제하지 않습니다.
    if not entries:
        print("기록 없음 - 삭제 없이 건너뜀:", day)
        return 0, 0, 0

    current_ids = {str(entry["food_entry_id"]) for entry in entries}
    existing = {
        fatsecret_id(page): page
        for page in notion_pages_for(day, RAW_DB_ID, "Date")
        if fatsecret_id(page)
    }
    added = updated = archived = 0

    for entry in entries:
        entry_id = str(entry["food_entry_id"])
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

    # 정상이며 비어 있지 않은 FatSecret 응답에서 사라진 ID만 휴지통으로 이동합니다.
    for entry_id, page in existing.items():
        if entry_id not in current_ids:
            write_page("PATCH", f"/pages/{page['id']}", {"archived": True})
            archived += 1
            time.sleep(0.4)

    summary_action = upsert_summary(day, entries)
    print("일일 요약", summary_action + ":", day)
    return added, updated, archived


def main():
    totals = [0, 0, 0]
    try:
        today = datetime.now(LOCAL_TIMEZONE).date()
        for offset in range(6, -1, -1):
            day = today - timedelta(days=offset)
            print("확인:", day)
            totals = [a + b for a, b in zip(totals, sync_day(day))]
            time.sleep(0.3)
    except Exception as exc:
        print("동기화 실패: 인증값과 응답 본문은 출력하지 않았습니다.")
        print("오류 종류:", type(exc).__name__)
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            print("실패 서비스:", urlparse(exc.response.url).hostname)
            print("HTTP 상태:", exc.response.status_code)
        raise SystemExit(1)

    print(
        f"완료 - 추가: {totals[0]}, 수정: {totals[1]}, "
        f"휴지통 이동: {totals[2]}"
    )


if __name__ == "__main__":
    main()
