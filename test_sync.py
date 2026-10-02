import os
import unittest
from copy import deepcopy
from datetime import date
from unittest.mock import patch

# Test-only placeholders; never load or print real authentication values.
for key in ("FATSECRET_CONSUMER_KEY", "FATSECRET_CONSUMER_SECRET", "FATSECRET_ACCESS_TOKEN", "FATSECRET_ACCESS_TOKEN_SECRET"):
    os.environ.setdefault(key, "test-only")
import sync

DAY = date(2026, 10, 2)
ENTRY = {"food_entry_id": "42", "food_entry_name": "test food", "meal": "Lunch", "calories": "100", "carbohydrate": "10", "protein": "10", "fat": "2"}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.pages = []
        self.writes = []
        self.patches = [patch.object(sync.time, "sleep"),
                        patch.object(sync, "notion_pages_for", side_effect=self.query),
                        patch.object(sync, "write_page", side_effect=self.write),
                        patch.object(sync, "upsert_summary", return_value="수정")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def query(self, day, database_id, date_property, entry_id=None):
        return deepcopy([p for p in self.pages if not p.get("archived") and
                         (sync.fatsecret_id(p) == entry_id if entry_id else p["properties"]["Date"]["date"]["start"] == day.isoformat())])

    def write(self, method, path, body):
        self.writes.append((method, path, deepcopy(body)))
        if method == "POST":
            self.pages.append({"id": str(len(self.pages) + 1), **deepcopy(body)})
        else:
            page = next(p for p in self.pages if p["id"] == path.split("/")[-1])
            page.update(deepcopy(body))

    def entries(self):
        return sync.normalize_entries([ENTRY])

    def test_same_record_in_both_outputs(self):
        e = self.entries()[0]
        props = sync.raw_properties_for(e, DAY)
        row = sync.food_records([e], DAY)[0]
        self.assertEqual(props["FatSecretID"]["rich_text"][0]["text"]["content"], row["fatsecret_id"])
        for prop, field in [("Calories", "calories"), ("Carbs", "carbs"), ("Protein", "protein"), ("Fat", "fat")]:
            self.assertEqual(props[prop]["number"], row[field])

    def test_retry_does_not_duplicate(self):
        sync.sync_day(DAY, self.entries())
        sync.sync_day(DAY, self.entries())
        self.assertEqual(len(self.pages), 1)

    def test_duplicate_rows_archived(self):
        sync.sync_day(DAY, self.entries())
        self.pages.append({**deepcopy(self.pages[0]), "id": "duplicate"})
        sync.sync_day(DAY, self.entries())
        self.assertEqual(len([p for p in self.pages if not p.get("archived")]), 1)

    def test_empty_authoritative_snapshot_archives(self):
        sync.sync_day(DAY, self.entries())
        sync.sync_day(DAY, [])
        self.assertTrue(self.pages[0]["archived"])

    def test_manual_row_without_fatsecret_id_preserved(self):
        props = sync.raw_properties_for(self.entries()[0], DAY)
        props["FatSecretID"] = {"rich_text": []}
        self.pages.append({"id": "manual", "properties": props})
        sync.sync_day(DAY, [])
        self.assertFalse(self.pages[0].get("archived", False))

    def test_date_move_reuses_same_page(self):
        sync.sync_day(date(2026, 10, 1), self.entries())
        sync.sync_day(date(2026, 10, 1), [], {"42"})
        sync.sync_day(DAY, self.entries(), {"42"})
        self.assertEqual(len(self.pages), 1)
        self.assertEqual(self.pages[0]["properties"]["Date"]["date"]["start"], DAY.isoformat())

    def test_invalid_numbers_and_conflicts_rejected(self):
        for value in ("NaN", "inf", "-1"):
            with self.assertRaises(ValueError):
                sync.normalize_entries([{**ENTRY, "calories": value}])
        with self.assertRaises(ValueError):
            sync.normalize_entries([ENTRY, {**ENTRY, "calories": "200"}])

    def test_duplicate_source_id_is_normalized_once(self):
        self.assertEqual(len(sync.normalize_entries([ENTRY, ENTRY])), 1)

    def test_notion_outage_does_not_block_supabase(self):
        with patch.object(sync, "entries_for", return_value=[ENTRY]), patch.object(sync, "sync_personal_os") as sb, patch.object(sync, "configure_notion", side_effect=RuntimeError("test")):
            with self.assertRaises(SystemExit):
                sync.main()
            self.assertEqual(sb.call_count, 7)

    def test_fatsecret_failure_does_not_send_empty_delete(self):
        with patch.object(sync, "entries_for", side_effect=RuntimeError("test")), patch.object(sync, "sync_personal_os") as sb, patch.object(sync, "configure_notion"):
            with self.assertRaises(SystemExit):
                sync.main()
            sb.assert_not_called()
            self.assertEqual(self.writes, [])

    def test_supabase_outage_does_not_block_notion(self):
        with patch.object(sync, "entries_for", return_value=[ENTRY]), patch.object(sync, "sync_personal_os", side_effect=RuntimeError("test")), patch.object(sync, "configure_notion"), patch.object(sync, "sync_day", return_value=(0, 1, 0, {})) as nt:
            with self.assertRaises(SystemExit):
                sync.main()
            self.assertEqual(nt.call_count, 7)


if __name__ == "__main__":
    unittest.main()
