from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main
from batch_rosters import export_batch_rosters
from daily_roster import roster_items
from manual_roster_order import apply_order, sync_manual_roster_order
from test_batch_rosters import fixture, roster, acknowledge_website, NOW, DAY, CID, LIMITS
from test_manual_roster_order import swap_order
from website_deletions import website_items, deleted_entries
from website_sync import sync_website
from withdrawal_sync import note_withdrawals


def add_order(record, label='A', account='manualuser', post_id='90001'):
    position = max([60] + [s['position'] for s in record['slots']]
                   + list(map(int, record['vacant_positions']))) + 1
    local = max([30] + [s['position'] for s in record['batches'][label]['slots']]
                + list(map(int, record['batches'][label]['vacant_positions']))) + 1
    return {'date': DAY, 'revision': record.get('manual_order_revision', 0) + 1,
            'baseRevision': record.get('manual_order_revision', 0), 'status': 'pending',
            'operation': 'add', 'moves': [], 'requestedAt': NOW.isoformat(),
            'addition': {'listId': label, 'position': position, 'listPosition': local,
                         'url': f'https://x.com/{account}/status/{post_id}',
                         'account': account, 'postId': post_id}}


class ManualAdditionTests(unittest.TestCase):
    def setUp(self):
        p = patch('batch_rosters.load_batch_config', return_value={'群一': {
            'enabled': True, 'effective_from': DAY, 'size': 30, 'max_batches': 2}})
        p.start()
        self.addCleanup(p.stop)

    def test_full_A_adds31_without_moving_B_and_survives_ten_refreshes(self):
        reg, state = fixture(61)
        before = deepcopy(roster(reg, state))
        changed = apply_order(before, add_order(before), DAY, reg)
        state['daily_rosters']['groups']['群一'] = changed
        self.assertEqual(len(changed['batches']['A']['slots']), 31)
        self.assertEqual(changed['batches']['B'], before['batches']['B'])
        for _ in range(10):
            state = json.loads(json.dumps(state))
            self.assertEqual(roster(reg, state), changed)
        self.assertNotIn('user61', changed['assigned_accounts'])
        self.assertEqual(changed['batch_policy'], before['batch_policy'])
        acknowledge_website(state)
        export_batch_rosters(reg, state)
        self.assertEqual(len(reg['batch_rosters']['groups']['群一']['batches']['A']['slots']), 31)

    def test_manual_bypass_keeps_automatic_qualification_and_quota(self):
        reg, state = fixture(10)
        before = roster(reg, state)
        reg['post_entries'][CID]['90001'] = {'promo_handle': 'manualuser', 'promo_post_id': '90001',
            'message_id': 999, 'tg_user_id': 999, 'time': f'{DAY} 02:00:00', 'mutual_eligible': False}
        changed = apply_order(before, add_order(before), DAY, reg)
        self.assertTrue(changed['batches']['A']['slots'][-1]['manual_addition'])
        state['daily_rosters']['groups']['群一'] = changed
        more, _ = fixture(40)
        reg['post_entries'][CID].update(more['post_entries'][CID])
        updated = roster(reg, state)
        self.assertEqual(len(updated['batches']['A']['slots']), 31)
        self.assertEqual(len(updated['batches']['B']['slots']), 10)
        self.assertEqual(sum(s['position'] <= 30 for s in updated['batches']['A']['slots']), 30)

    def test_two_independent_extra_positions_and_swap_preserve_membership(self):
        reg, state = fixture(60)
        first = apply_order(roster(reg, state), add_order(roster(reg, state)), DAY, reg)
        second = apply_order(first, add_order(first, 'B', 'manualtwo', '90002'), DAY, reg)
        state['daily_rosters']['groups']['群一'] = second
        self.assertEqual(roster(reg, state), second)
        extras = [x for x in roster_items(second) if x.get('manualAddition')]
        self.assertEqual([(x['position'], x['listId'], x['listPosition']) for x in extras], [(61, 'A', 31), (62, 'B', 31)])
        swapped = apply_order(second, swap_order(second, 1, 62, 3), DAY, reg)
        state['daily_rosters']['groups']['群一'] = swapped
        self.assertEqual(roster(reg, state), swapped)
        moved = next(s for s in swapped['slots'] if s['handle'] == 'manualtwo')
        self.assertEqual((moved['list_id'], moved['list_position']), ('A', 1))

    def test_extra_deletion_has_no_automatic_replacement_and_keeps_tombstone(self):
        reg, state = fixture(61)
        entry = reg['post_entries'][CID]['10061']
        before = roster(reg, state)
        changed = apply_order(before, add_order(before, 'A', 'user61', '10061'), DAY, reg)
        state['daily_rosters']['groups']['群一'] = changed
        note_withdrawals(reg, CID, {'61': 'deleted'}, NOW)
        extra, _ = fixture(62)
        reg['post_entries'][CID]['10062'] = extra['post_entries'][CID]['10062']
        result = roster(reg, state)
        self.assertNotIn('user62', result['assigned_accounts'])
        deleted = next(x for x in website_items(result, deleted_entries(state, '群一', DAY)) if x['position'] == 61)
        self.assertEqual((deleted['url'], deleted['listId'], deleted['listPosition'], deleted['manualAddition']), (None, 'A', 31, True))

    def test_pending_private_edit_failure_does_not_publish_or_export_added_member(self):
        reg, state = fixture(30)
        with patch.dict('os.environ', {'TELEGRAM_OWNER_CHAT_ID': '12345'}), patch.object(
            main, '_send_private_message', return_value=(True, {'message_id': 900})
        ), patch.object(main, '_edit_private_message', return_value=(False, 'retry')) as edit:
            main.send_daily_lists_to_owner(reg, state, NOW, limits=LIMITS)
            acknowledge_website(state)
            export_batch_rosters(reg, state)
            before = deepcopy(reg['batch_rosters'])
            old = state['daily_rosters']['groups']['群一']
            state['daily_rosters']['groups']['群一'] = apply_order(old, add_order(old), DAY, reg)
            main.send_daily_lists_to_owner(reg, state, NOW, limits=LIMITS)
            self.assertEqual(edit.call_count, 1)
            post = Mock()
            self.assertEqual(sync_website(state, NOW, secret='test', post=post)['群一'], 'awaiting_order_private_edit')
            post.assert_not_called()
            export_batch_rosters(reg, state)
            self.assertEqual(reg['batch_rosters'], before)

    def test_added_link_updates_existing_private_message_once_and_exports(self):
        reg, state = fixture(30)
        with patch.dict('os.environ', {'TELEGRAM_OWNER_CHAT_ID': '12345'}), patch.object(
            main, '_send_private_message', return_value=(True, {'message_id': 900})
        ) as send, patch.object(main, '_edit_private_message', return_value=(True, '')) as edit:
            main.send_daily_lists_to_owner(reg, state, NOW, limits=LIMITS)
            old = state['daily_rosters']['groups']['群一']
            state['daily_rosters']['groups']['群一'] = apply_order(old, add_order(old), DAY, reg)
            main.send_daily_lists_to_owner(reg, state, NOW, limits=LIMITS)
            main.send_daily_lists_to_owner(reg, state, NOW, limits=LIMITS)
            self.assertEqual((send.call_count, edit.call_count), (1, 1))
            self.assertIn('31 https://x.com/manualuser/status/90001', edit.call_args.args[2])
        acknowledge_website(state)
        export_batch_rosters(reg, state)
        self.assertEqual(reg['batch_rosters']['groups']['群一']['count'], 31)

    def test_next_day_does_not_inherit_manual_extra(self):
        from batch_rosters import refresh_batch_roster
        reg, state = fixture(60)
        old = apply_order(roster(reg, state), add_order(roster(reg, state)), DAY, reg)
        next_day = '2026-09-29'
        tomorrow = deepcopy(reg)
        tomorrow['date'] = next_day
        for e in tomorrow['post_entries'][CID].values():
            e['time'] = e['time'].replace(DAY, next_day)
        fresh = refresh_batch_roster(None, tomorrow, '群一', next_day, next_day+' 12:00:00', old['batch_policy'])
        self.assertEqual([len(b['slots']) for b in fresh['batches'].values()], [30, 30])
        self.assertNotIn('manualuser', fresh['assigned_accounts'])

    def test_invalid_duplicate_or_other_date_requests_are_atomic(self):
        reg, state = fixture(60)
        before = deepcopy(roster(reg, state))
        for key, value in [('listId', 'Z'), ('account', 'user1'), ('postId', 'wrong'), ('position', 31), ('listPosition', 1)]:
            order = add_order(before)
            order['addition'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                apply_order(before, order, DAY, reg)
        with self.assertRaises(ValueError):
            apply_order(before, add_order(before), '2026-09-29', reg)
        self.assertEqual(before, state['daily_rosters']['groups']['群一'])

    def test_signed_pull_adds_once_even_after_lost_commit(self):
        reg, state = fixture(30)
        order = add_order(roster(reg, state))
        post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {'order': order}))
        self.assertEqual(sync_manual_roster_order(state, NOW, secret='test', post=post, registry=reg), 'applied')
        self.assertEqual(sync_manual_roster_order(state, NOW, secret='test', post=post, registry=reg), 'unchanged')
        self.assertEqual(state['daily_rosters']['groups']['群一']['count'], 31)


if __name__ == '__main__':
    unittest.main()
