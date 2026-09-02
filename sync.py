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
    r = requests.get(
        FS_URL,
        params={"date": (day - date(1970, 1, 1)).days, "format": "json"},
        auth=oauth,
        timeout=30,
    )
    r.raise_for_status()
    value = r.json().get("food_entries", {}).get("food_entry", [])
    return [value] if isinstance(value, dict) else value


def exists(entry_id):
    r = requests.post(
        f"{NOTION_URL}/databases/{DB_ID}/query",
        headers=headers,
        json={
            "page_size": 1,
            "filter": {
                "property": "FatSecretID",
                "rich_text": {"equals": str(entry_id)},
            },
        },
        timeout=30,
    )
    r.raise_for_status()
    return bool(r.json().get("results"))


def add(entry, day):
    name = str(entry.get("food_entry_name", "이름 없음"))
    meal = {
        "Breakfast": "아침",
        "Lunch": "점심",
        "Dinner": "저녁",
    }.get(str(entry.get("meal", "")), "간식")
    properties = {
        "Name": {"title": [{"text": {"content": name}}]},
        "Meal": {"select": {"name": meal}},
        "Date": {"date": {"start": day.isoformat()}},
        "Calories": {"number": float(entry.get("calories") or 0)},
        "Carbs": {"number": float(entry.get("carbohydrate") or 0)},
        "Protein": {"number": float(entry.get("protein") or 0)},
        "Fat": {"number": float(entry.get("fat") or 0)},
        "Food": {"rich_text": [{"text": {"content": name}}]},
        "FatSecretID": {
            "rich_text": [{"text": {"content": str(entry["food_entry_id"])}}]
        },
    }
    r = requests.post(
        f"{NOTION_URL}/pages",
        headers=headers,
        json={"parent": {"database_id": DB_ID}, "properties": properties},
        timeout=30,
    )
    r.raise_for_status()


def main():
    added = skipped = 0
    try:
        for offset in range(6, -1, -1):
            day = date.today() - timedelta(days=offset)
            print("확인:", day)
            for entry in entries_for(day):
                if exists(entry["food_entry_id"]):
                    skipped += 1
                    continue
                add(entry, day)
                added += 1
                print("추가:", entry.get("food_entry_name"))
                time.sleep(0.4)
            time.sleep(0.3)
    except Exception:
        print("동기화 실패: 인증값은 출력하지 않았습니다.")
        raise SystemExit(1)
    print(f"완료 - 추가: {added}, 이미 존재: {skipped}")


if __name__ == "__main__":
    main()
