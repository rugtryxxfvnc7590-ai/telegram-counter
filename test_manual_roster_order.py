from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import main
from batch_rosters import export_batch_rosters
from manual_roster_order import apply_order, sync_manual_roster_order
from test_batch_rosters import fixture, roster, acknowledge_website, NOW, DAY, CID, LIMITS
from website_sync import sync_website
from withdrawal_sync import note_withdrawals


def swap_order(record, first=1, second=31, revision=1):
    by_position = {slot['position']: slot for slot in record['slots']}
    return {'date': DAY, 'revision': revision, 'baseRevision': record.get('manual_order_revision', 0),
            'status': 'pending', 'moves': [
                {'account': by_position[a]['handle'].lower(), 'postId': by_position[a]['post_id'], 'from': a, 'to': b}
                for a, b in ((first, second), (second, first))]}


class ManualRosterOrderTests(unittest.TestCase):
    def setUp(self):
        policy = patch('batch_rosters.load_batch_config', return_value={'群一': {
            'enabled': True, 'effective_from': DAY, 'size': 30, 'max_batches': 2}})
        policy.start()
        self.addCleanup(policy.stop)

    def test_cross_batch_swap_persists_ten_refreshes_and_exports_checker_groups(self):
        reg, state = fixture(39)
        before = deepcopy(roster(reg, state))
        order = swap_order(before)
        new = apply_order(before, order, DAY)
        state['daily_rosters']['groups']['群一'] = new
        self.assertEqual(before['count'], new['count'])
        self.assertEqual([len(b['slots']) for b in new['batches'].values()], [30, 9])
        for move in order['moves']:
            slot = next(s for s in new['slots'] if s['handle'] == move['account'])
            self.assertEqual(slot['position'], move['to'])
            self.assertEqual(new['assigned_accounts'][slot['handle']], slot['list_id'])
        for _ in range(10):
            state = json.loads(json.dumps(state))
            self.assertEqual(roster(reg, state), new)
        acknowledge_website(state)
        export_batch_rosters(reg, state)
        self.assertEqual(reg['batch_rosters']['groups']['群一']['batches'], new['batches'])
        self.assertEqual(apply_order(new, order, DAY), new)

    def test_same_batch_swap_and_second_request_keep_prior_pins(self):
        reg, state = fixture(39)
        first = apply_order(roster(reg, state), swap_order(roster(reg, state), 1, 2), DAY)
        second = apply_order(first, swap_order(first, 3, 31, 2), DAY)
        state['daily_rosters']['groups']['群一'] = second
        self.assertEqual(roster(reg, state), second)
        self.assertEqual(len([s for s in second['slots'] if s.get('manual_position')]), 4)

    def test_wrong_identity_position_date_or_duplicate_rejects_without_mutation(self):
        reg, state = fixture(31)
        record = deepcopy(roster(reg, state))
        for key, value in [('account', 'someone_else'), ('postId', '1'), ('from', 3), ('to', 99)]:
            order = swap_order(record)
            order['moves'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                apply_order(record, order, DAY)
        order = swap_order(record)
        order['moves'].append(deepcopy(order['moves'][0]))
        with self.assertRaises(ValueError):
            apply_order(record, order, DAY)
        with self.assertRaises(ValueError):
            apply_order(record, swap_order(record), '2026-09-29')
        self.assertEqual(record, state['daily_rosters']['groups']['群一'])

    def test_private_messages_edit_in_place_after_swap_not_duplicate_send(self):
        reg, state = fixture(39)
        with patch.dict('os.environ', {'TELEGRAM_OWNER_CHAT_ID': '12345'}), patch.object(
            main, '_send_private_message', side_effect=[(True, {'message_id': 901}), (True, {'message_id': 902})]
        ) as send, patch.object(main, '_edit_private_message', return_value=(True, '')) as edit:
            main.send_daily_lists_to_owner(reg, state, NOW.replace(hour=19), limits=LIMITS)
            current = state['daily_rosters']['groups']['群一']
            order = swap_order(current)
            state['daily_rosters']['groups']['群一'] = apply_order(current, order, DAY)
            main.send_daily_lists_to_owner(reg, state, NOW.replace(hour=20), limits=LIMITS)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(edit.call_count, 2)
            main.send_daily_lists_to_owner(reg, state, NOW.replace(hour=21), limits=LIMITS)
            self.assertEqual(edit.call_count, 2)
        delivery = state['owner_daily_lists']['groups']['群一']['batches']
        self.assertEqual([delivery[x]['message_id'] for x in ('A', 'B')], [901, 902])
        self.assertEqual(delivery['A']['slots'][0]['handle'], order['moves'][1]['account'])

    def test_deletion_and_candidate_fill_new_position_not_old_batch(self):
        reg, state = fixture(61)
        record = roster(reg, state)
        order = swap_order(record)
        moved = next(s for s in record['slots'] if s['position'] == 1)
        state['daily_rosters']['groups']['群一'] = apply_order(record, order, DAY)
        reg['post_entries'][CID].pop(moved['post_id'])
        note_withdrawals(reg, CID, {moved['message_id']: 'deleted'}, NOW)
        changed = roster(reg, state)
        candidate = next(s for s in changed['slots'] if s['handle'] == 'user61')
        self.assertEqual((candidate['list_id'], candidate['position']), ('B', 31))

    def test_signed_pull_apply_and_lost_commit_recovery_then_sync_revision(self):
        reg, state = fixture(31)
        order = swap_order(roster(reg, state))
        for status in ('pending', 'applied'):
            current = deepcopy(state)
            response = SimpleNamespace(status_code=200, json=lambda: {'order': dict(order, status=status)})
            post = Mock(return_value=response)
            self.assertEqual(sync_manual_roster_order(current, NOW, secret='test', post=post), 'applied')
            self.assertEqual(sync_manual_roster_order(current, NOW, secret='test', post=post), 'unchanged')
            self.assertIn('X-Sync-Signature', post.call_args.kwargs['headers'])
            sync = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {'status': 'updated'}))
            sync_website(current, NOW, secret='test', post=sync)
            self.assertEqual(json.loads(sync.call_args.kwargs['data'])['manualOrderRevision'], 1)

    def test_failed_pull_or_stale_plan_keeps_roster_and_reports_rejection(self):
        reg, state = fixture(31)
        original = deepcopy(roster(reg, state))
        self.assertEqual(sync_manual_roster_order(state, NOW, secret='test', post=Mock(side_effect=TimeoutError())), 'retry')
        order = swap_order(original)
        order['moves'][0]['postId'] = 'wrong'
        post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {'order': order}))
        self.assertEqual(sync_manual_roster_order(state, NOW, secret='test', post=post), 'roster_changed')
        self.assertEqual(json.loads(post.call_args.kwargs['data'])['status'], 'rejected')
        self.assertEqual(state['daily_rosters']['groups']['群一'], original)

    def test_private_edit_failure_keeps_public_and_checker_on_old_groups(self):
        reg, state = fixture(39)
        with patch.dict('os.environ', {'TELEGRAM_OWNER_CHAT_ID': '12345'}), patch.object(
            main, '_send_private_message', return_value=(True, {'message_id': 900})
        ), patch.object(main, '_edit_private_message', return_value=(False, 'retry')):
            main.send_daily_lists_to_owner(reg, state, NOW.replace(hour=19), limits=LIMITS)
            export_batch_rosters(reg, state)
            before = deepcopy(reg['batch_rosters'])
            current = state['daily_rosters']['groups']['群一']
            state['daily_rosters']['groups']['群一'] = apply_order(current, swap_order(current), DAY)
            main.send_daily_lists_to_owner(reg, state, NOW.replace(hour=20), limits=LIMITS)
            sender = Mock()
            self.assertEqual(sync_website(state, NOW.replace(hour=20), secret='test', post=sender)['群一'], 'awaiting_order_private_edit')
            sender.assert_not_called()
            export_batch_rosters(reg, state)
            self.assertEqual(reg['batch_rosters'], before)


if __name__ == '__main__':
    unittest.main()
