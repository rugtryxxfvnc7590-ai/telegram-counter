from copy import deepcopy
import unittest

from private_list_sync import order_roster_slots
from daily_roster import refresh_daily_rosters
from test_website_roster import CID, DAY, NOW, LIMITS, entry, fixture


class RosterOrderTests(unittest.TestCase):
    def test_incremental_and_all_at_once_collection_have_identical_order(self):
        registry, state = fixture(0)
        for batch in ([1, 2], [3, 4], [5]):
            for n in batch:
                registry["post_entries"][CID][str(1000+n)] = entry(n)
            refresh_daily_rosters(registry, state, LIMITS, NOW)
        _, once = fixture(5)
        self.assertEqual(state["daily_rosters"], once["daily_rosters"])
        before = deepcopy(state["daily_rosters"])
        for _ in range(10):
            refresh_daily_rosters(registry, state, LIMITS, NOW)
            self.assertEqual(state["daily_rosters"], before)

    def test_legacy_mixed_order_corrected_without_changing_members_or_links(self):
        slots = [{"position": i, "message_id": str(mid), "time": f"{DAY} 01:{mid:02d}:00",
                  "url": f"https://x.com/person{mid}/status/{mid}"}
                 for i, mid in enumerate([2, 1, 3, 4], 1)]
        ordered = order_roster_slots(slots)
        self.assertEqual([s["message_id"] for s in ordered], ["4", "3", "2", "1"])
        self.assertEqual({s["url"] for s in ordered}, {s["url"] for s in slots})
        self.assertEqual([s["message_id"] for s in slots], ["2", "1", "3", "4"])

    def test_pending_edit_time_orders_but_admitted_edit_never_moves(self):
        slots = [{"position": 1, "message_id": "2", "time": f"{DAY} 02:00:00"},
                 {"position": 2, "message_id": "1", "time": f"{DAY} 01:00:00",
                  "admission_time": f"{DAY} 03:00:00"}]
        first = order_roster_slots(slots)
        self.assertEqual(first[0]["message_id"], "1")
        slots[0].update(edited=True, edit_time=f"{DAY} 18:00:00", url="https://x.com/p/status/999")
        self.assertEqual([s["message_id"] for s in order_roster_slots(slots)], ["1", "2"])

    def test_real_candidate_and_vacancy_keep_original_numbers(self):
        slots = [{"position": pos, "message_id": str(mid), "time": f"{DAY} 01:{mid:02d}:00"}
                 for pos, mid in [(1, 2), (2, 9), (4, 1), (5, 3)]]
        slots[1]["is_replacement"] = True
        result = order_roster_slots(slots, {"3": "retired"})
        self.assertEqual([(s["position"], s["message_id"]) for s in result], [(1, "3"), (2, "9"), (4, "2"), (5, "1")])
        self.assertEqual(order_roster_slots(result, {"3": "retired"}), result)

    def test_same_second_uses_numeric_message_id(self):
        slots = [{"position": i, "message_id": mid, "time": f"{DAY} 01:00:00"}
                 for i, mid in enumerate(["9", "10", "11"], 1)]
        self.assertEqual([s["message_id"] for s in order_roster_slots(slots)], ["11", "10", "9"])


if __name__ == "__main__":
    unittest.main()
