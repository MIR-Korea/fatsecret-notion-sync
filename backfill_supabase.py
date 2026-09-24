import argparse
import os
import time
from datetime import date, datetime, timedelta
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


oauth = OAuth1(secret("FATSECRET_CONSUMER_KEY"), client_secret=secret("FATSECRET_CONSUMER_SECRET"), resource_owner_key=secret("FATSECRET_ACCESS_TOKEN"), resource_owner_secret=secret("FATSECRET_ACCESS_TOKEN_SECRET"), signature_method="HMAC-SHA1")


def oidc_token():
    url = secret("ACTIONS_ID_TOKEN_REQUEST_URL")
    token = secret("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    sep = "&" if "?" in url else "?"
    r = requests.get(f"{url}{sep}audience=personal-os-supabase", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()["value"]


def entries_for(day):
    r = requests.get(FS_URL, params={"date": (day - date(1970, 1, 1)).days, "format": "json"}, auth=oauth, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        code = str(data["error"].get("code", "unknown"))
        raise RuntimeError(f"FatSecret error code {code}")
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
    r = requests.post(INGEST_URL, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, json=payload, timeout=60)
    r.raise_for_status()
    result = r.json()
    if not result.get("ok"):
        raise RuntimeError("Supabase ingest rejected payload")
    return result


def checkpoint(token):
    return post({"get_checkpoint": True}, token).get("last_completed_date")


def parse_args():
    today = datetime.now(LOCAL_TIMEZONE).date()
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2023-03-28")
    p.add_argument("--end", default=today.isoformat())
    p.add_argument("--sleep", type=float, default=0.35)
    p.add_argument("--ignore-checkpoint", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    requested_start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    token = oidc_token()
    saved = None if args.ignore_checkpoint else checkpoint(token)
    start = requested_start
    if saved:
        resume = date.fromisoformat(saved) + timedelta(days=1)
        if resume > start:
            start = resume
    if start > end:
        print(f"DONE already complete checkpoint={saved} end={end}")
        return
    print(f"START requested={requested_start} resume={start} end={end} checkpoint={saved}")
    checked = entry_count = food_days = 0
    current = start
    while current <= end:
        try:
            entries = entries_for(current)
        except RuntimeError as exc:
            if "FatSecret error code 12" in str(exc):
                print(f"PAUSED rate-limit at={current} checked_this_run={checked}; rerun the workflow to resume")
                return
            raise
        day_payload = {"date": current.isoformat(), "calories": round(sum(num(e, "calories") for e in entries), 2), "carbs": round(sum(num(e, "carbohydrate") for e in entries), 2), "protein": round(sum(num(e, "protein") for e in entries), 2), "fat": round(sum(num(e, "fat") for e in entries), 2), "food_count": len(entries)}
        if entries:
            foods = [{"fatsecret_id": str(e["food_entry_id"]), "date": current.isoformat(), "food": str(e.get("food_entry_name", "이름 없음")), "meal": meal_name(e.get("meal")), "calories": num(e, "calories"), "carbs": num(e, "carbohydrate"), "protein": num(e, "protein"), "fat": num(e, "fat")} for e in entries]
            for i in range(0, len(foods), 200):
                result = post({"entries": foods[i:i + 200]}, token)
                entry_count += int(result.get("upserted_entries", 0))
            post({"days": [day_payload]}, token)
            food_days += 1
        # Advance checkpoint only after this date was successfully read and any data was persisted.
        post({"checkpoint_date": current.isoformat()}, token)
        checked += 1
        if checked % 10 == 0:
            print(f"progress: {current} checked={checked} food_days={food_days} entries={entry_count}")
        current += timedelta(days=1)
        time.sleep(max(args.sleep, 0))
    print(f"DONE checked_this_run={checked} food_days={food_days} upserted_entries={entry_count} range={start}..{end}")


if __name__ == "__main__":
    main()
