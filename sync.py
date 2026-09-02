import os
import time
from datetime import date, timedelta
from urllib.parse import urlparse

import requests
from requests_oauthlib import OAuth1

FS_URL = "https://platform.fatsecret.com/rest/food-entries/v2"
NOTION_URL = "https://api.notion.com/v1"
DB_ID = os.environ["NOTION_DATABASE_ID"]
oauth = OAuth1(
    os.environ["FATSECRET_CONSUMER_KEY"],
    client_secret=os.environ["FATSECRET_CONSUMER_SECRET"],
    resource_owner_key=os.environ["FATSECRET_ACCESS_TOKEN"],
    resource_owner_secret=os.environ["FATSECRET_ACCESS_TOKEN_SECRET"],
    signature_method="HMAC-SHA1",
    signature_type="QUERY",
)
headers = {
    "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def entries_for(day):
    r = requests.get(FS_URL, params={
        "date": (day - date(1970, 1, 1)).days, "format": "json"
    }, auth=oauth, timeout=30)
    r.raise_for_status()
    data = r.json()

    if data.get("error"):
        code = data["error"].get("code", "unknown")
        print("FatSecret API 오류 코드:", code)
        raise RuntimeError("FatSecret API 오류")

    value = data.get("food_entries", {}).get("food_entry", [])
    return [value] if isinstance(value, dict) else value


def notion_pages_for(day):
    pages, cursor = [], None
    while True:
        body = {"page_size": 100, "filter": {
            "property": "Date", "date": {"equals": day.isoformat()}
        }}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(f"{NOTION_URL}/databases/{DB_ID}/query",
                          headers=headers, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            return pages
        cursor = data["next_cursor"]


def fatsecret_id(page):
    values = page.get("properties", {}).get(
        "FatSecretID", {}).get("rich_text", [])
    return values[0].get("plain_text", "") if values else ""


def properties_for(entry, day):
    name = str(entry.get("food_entry_name", "이름 없음"))
    meal = {"Breakfast": "아침", "Lunch": "점심",
            "Dinner": "저녁"}.get(str(entry.get("meal", "")), "간식")
    return {
        "Name": {"title": [{"text": {"content": name}}]},
        "Meal": {"select": {"name": meal}},
        "Date": {"date": {"start": day.isoformat()}},
        "Calories": {"number": float(entry.get("calories") or 0)},
        "Carbs": {"number": float(entry.get("carbohydrate") or 0)},
        "Protein": {"number": float(entry.get("protein") or 0)},
        "Fat": {"number": float(entry.get("fat") or 0)},
        "Food": {"rich_text": [{"text": {"content": name}}]},
        "FatSecretID": {"rich_text": [{
            "text": {"content": str(entry["food_entry_id"])}
        }]},
    }


def write_page(method, path, body):
    r = requests.request(method, f"{NOTION_URL}{path}",
                         headers=headers, json=body, timeout=30)
    r.raise_for_status()


def sync_day(day):
    entries = entries_for(day)

    # 빈 응답은 정상적인 빈 기록인지 API 이상인지 구분하기 어려우므로
    # 안전을 위해 Notion 조회와 수정을 모두 건너뜁니다.
    if not entries:
        print("기록 없음 - 안전하게 건너뜀:", day)
        return 0, 0

    existing = {fatsecret_id(page): page for page in notion_pages_for(day)
                if fatsecret_id(page)}
    added = updated = 0

    for entry in entries:
        entry_id = str(entry["food_entry_id"])
        if entry_id in existing:
            write_page("PATCH", f"/pages/{existing[entry_id]['id']}",
                       {"properties": properties_for(entry, day)})
            updated += 1
        else:
            write_page("POST", "/pages", {
                "parent": {"database_id": DB_ID},
                "properties": properties_for(entry, day),
            })
            added += 1
        time.sleep(0.4)

    return added, updated


def main():
    totals = [0, 0]
    try:
        for offset in range(6, -1, -1):
            day = date.today() - timedelta(days=offset)
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

    print(f"완료 - 추가: {totals[0]}, 수정: {totals[1]}")


if __name__ == "__main__":
    main()
