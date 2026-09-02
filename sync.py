import os
import time
from datetime import date, timedelta

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
)
headers = {
    "Authorization": f"Bearer {os.environ['NOTION_TOKEN']}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}


def entries_for(day):
    response = requests.get(
        FS_URL,
        params={"date": (day - date(1970, 1, 1)).days, "format": "json"},
        auth=oauth,
        timeout=30,
    )
    response.raise_for_status()
    value = response.json().get("food_entries", {}).get("food_entry", [])
    return [value] if isinstance(value, dict) else value


def notion_pages_for(day):
    pages = []
    cursor = None
    while True:
        body = {
            "page_size": 100,
            "filter": {"property": "Date", "date": {"equals": day.isoformat()}},
        }
        if cursor:
            body["start_cursor"] = cursor
        response = requests.post(
            f"{NOTION_URL}/databases/{DB_ID}/query",
            headers=headers,
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            return pages
        cursor = data["next_cursor"]


def fatsecret_id(page):
    values = page.get("properties", {}).get("FatSecretID", {}).get("rich_text", [])
    return values[0].get("plain_text", "") if values else ""


def properties_for(entry, day):
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


def create_page(entry, day):
    response = requests.post(
        f"{NOTION_URL}/pages",
        headers=headers,
        json={
            "parent": {"database_id": DB_ID},
            "properties": properties_for(entry, day),
        },
        timeout=30,
    )
    response.raise_for_status()


def update_page(page_id, entry, day):
    response = requests.patch(
        f"{NOTION_URL}/pages/{page_id}",
        headers=headers,
        json={"properties": properties_for(entry, day)},
        timeout=30,
    )
    response.raise_for_status()


def archive_page(page_id):
    response = requests.patch(
        f"{NOTION_URL}/pages/{page_id}",
        headers=headers,
        json={"archived": True},
        timeout=30,
    )
    response.raise_for_status()


def sync_day(day):
    entries = entries_for(day)
    current_ids = {str(entry["food_entry_id"]) for entry in entries}
    existing = {
        fatsecret_id(page): page
        for page in notion_pages_for(day)
        if fatsecret_id(page)
    }

    added = updated = archived = 0

    for entry in entries:
        entry_id = str(entry["food_entry_id"])
        if entry_id in existing:
            update_page(existing[entry_id]["id"], entry, day)
            updated += 1
        else:
            create_page(entry, day)
            added += 1
        time.sleep(0.4)

    for entry_id, page in existing.items():
        if entry_id not in current_ids:
            archive_page(page["id"])
            archived += 1
            time.sleep(0.4)

    return added, updated, archived


def main():
    totals = [0, 0, 0]
    try:
        for offset in range(6, -1, -1):
            day = date.today() - timedelta(days=offset)
            print("확인:", day)
            result = sync_day(day)
            totals = [a + b for a, b in zip(totals, result)]
            time.sleep(0.3)
    except Exception:
        print("동기화 실패: 인증값과 응답 본문은 출력하지 않았습니다.")
        raise SystemExit(1)

    print(
        f"완료 - 추가: {totals[0]}, 수정: {totals[1]}, "
        f"휴지통 이동: {totals[2]}"
    )


if __name__ == "__main__":
    main()
