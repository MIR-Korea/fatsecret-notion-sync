import argparse
import os
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from requests_oauthlib import OAuth1

FS_URL = "https://platform.fatsecret.com/rest/food-entries/v2"
INGEST_URL = "https://gobpgixochawblszmqob.supabase.co/functions/v1/personal-os-nutrition-ingest"
LOCAL_TIMEZONE = ZoneInfo("Asia/Seoul")


def secret(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing secret: {name}")
    return value


oauth = OAuth1(
    secret("FATSECRET_CONSUMER_KEY"),
    client_secret=secret("FATSECRET_CONSUMER_SECRET"),
    resource_owner_key=secret("FATSECRET_ACCESS_TOKEN"),
    resource_owner_secret=secret("FATSECRET_ACCESS_TOKEN_SECRET"),
    signature_method="HMAC-SHA1",
)


def oidc_token():
    url = secret("ACTIONS_ID_TOKEN_REQUEST_URL")
    token = secret("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    sep = "&" if "?" in url else "?"
    r = requests.get(
        f"{url}{sep}audience=personal-os-supabase",
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    r.raise_for_status()
    return r.json()["value"]


def entries_for(day):
    r = requests.get(
        FS_URL,
        params={"date": (day - date(1970, 1, 1)).days, "format": "json"},
        auth=oauth, timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"FatSecret error code {data['error'].get('code', 'unknown')}")
    container = data.get("food_entries") or {}
    value = container.get("food_entry")
    if value is None:
        return []
    return [value] if isinstance(value, dict) else value


def num(entry, key):
    return round(float(entry.get(key) or 0), 2)


def meal_name(value):
    return {"Breakfast": "아침", "Lunch": "점심", "Dinner": "저녁"}.get(str(value), "간식")


def post(payload, token):
    r = requests.post(
        INGEST_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload, timeout=60,
    )
    r.raise_for_status()
    result = r.json()
    if not result.get("ok"):
        raise RuntimeError("Supabase ingest rejected payload")
    return result


def parse_args():
    today = datetime.now(LOCAL_TIMEZONE).date()
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2023-03-28")
    p.add_argument("--end", default=today.isoformat())
    p.add_argument("--sleep", type=float, default=0.15)
    return p.parse_args()


def main():
    args = parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end < start:
        raise SystemExit("end must be >= start")

    token = oidc_token()
    day_count = entry_count = days_with_food = 0
    current = start
    while current <= end:
        entries = entries_for(current)
        day_payload = {
            "date": current.isoformat(),
            "calories": round(sum(num(e, "calories") for e in entries), 2),
            "carbs": round(sum(num(e, "carbohydrate") for e in entries), 2),
            "protein": round(sum(num(e, "protein") for e in entries), 2),
            "fat": round(sum(num(e, "fat") for e in entries), 2),
            "food_count": len(entries),
        }
        if entries:
            food_payload = [{
                "fatsecret_id": str(e["food_entry_id"]),
                "date": current.isoformat(),
                "food": str(e.get("food_entry_name", "이름 없음")),
                "meal": meal_name(e.get("meal")),
                "calories": num(e, "calories"),
                "carbs": num(e, "carbohydrate"),
                "protein": num(e, "protein"),
                "fat": num(e, "fat"),
            } for e in entries]
            for i in range(0, len(food_payload), 200):
                result = post({"entries": food_payload[i:i + 200]}, token)
                entry_count += int(result.get("upserted_entries", 0))
            post({"days": [day_payload]}, token)
            days_with_food += 1
        day_count += 1
        if day_count % 30 == 0:
            print(f"progress: {current} checked={day_count} food_days={days_with_food} entries={entry_count}")
        current = date.fromordinal(current.toordinal() + 1)
        time.sleep(max(args.sleep, 0))

    print(f"DONE checked_days={day_count} food_days={days_with_food} upserted_entries={entry_count} range={start}..{end}")


if __name__ == "__main__":
    main()
