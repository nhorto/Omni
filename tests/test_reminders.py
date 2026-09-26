from datetime import datetime
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

import reminders


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'reminders.db'
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        reminders.setup(self.db)
        self.db.commit()

    def test_restart_catches_up_without_redelivering_completed_reminders(self):
        ident = reminders.add(self.db, 'Synthetic reminder', 150, clock=100)
        sender = Mock(return_value='42')
        with sqlite3.connect(self.path) as restarted:
            self.assertEqual(reminders.deliver_due(restarted, {}, clock=200, sender=sender), 1)
        self.assertEqual(reminders.deliver_due(self.db, {}, clock=300, sender=sender), 0)
        sender.assert_called_once_with('Synthetic reminder', ident)

    def test_proposals_require_review_and_source_events_are_deduplicated(self):
        ident = reminders.add(self.db, 'Suggested reminder', 150, proposed=True, source_key='synthetic:event-1', clock=100)
        duplicate = reminders.add(self.db, 'Duplicate intake', 150, proposed=True, source_key='synthetic:event-1', clock=100)
        self.assertEqual(duplicate, ident)
        sender = Mock(return_value='1')
        reminders.deliver_due(self.db, {}, clock=200, sender=sender)
        sender.assert_not_called()
        reminders.review(self.db, ident, True)
        self.assertEqual(reminders.deliver_due(self.db, {}, clock=200, sender=sender), 1)

    def test_failed_delivery_retries_without_losing_the_reminder(self):
        ident = reminders.add(self.db, 'Retry me', 150, clock=100)
        failing = Mock(side_effect=RuntimeError('Synthetic notification service failure'))
        self.assertEqual(reminders.deliver_due(self.db, {}, clock=200, sender=failing), 0)
        self.assertEqual(reminders.deliver_due(self.db, {}, clock=201, sender=failing), 0)
        self.assertEqual(failing.call_count, 1)
        working = Mock(return_value='2')
        self.assertEqual(reminders.deliver_due(self.db, {}, clock=231, sender=working), 1)
        self.assertEqual(self.db.execute('SELECT status FROM reminders WHERE id=?', (ident,)).fetchone()[0], 'delivered')

    def test_cancelled_reminder_never_delivers(self):
        ident = reminders.add(self.db, 'Cancel me', 150, clock=100)
        reminders.cancel(self.db, ident)
        sender = Mock()
        self.assertEqual(reminders.deliver_due(self.db, {}, clock=200, sender=sender), 0)
        sender.assert_not_called()

    def test_quiet_hours_cross_midnight_and_preserve_due_items(self):
        clock = datetime(2026, 1, 1, 23, 0).astimezone().timestamp()
        reminders.add(self.db, 'Wait for morning', clock - 1, clock=clock - 100)
        config = dict(quiet_hours_enabled=True, quiet_hours_start='22:00', quiet_hours_end='08:00')
        sender = Mock(return_value='3')
        self.assertEqual(reminders.deliver_due(self.db, config, clock=clock, sender=sender), 0)
        morning = datetime(2026, 1, 2, 8, 0).astimezone().timestamp()
        self.assertEqual(reminders.deliver_due(self.db, config, clock=morning, sender=sender), 1)

    def test_expired_dispatch_claim_is_recovered(self):
        ident = reminders.add(self.db, 'Interrupted reminder', 150, clock=100)
        self.db.execute("UPDATE reminders SET status='dispatching',claimed_at=200 WHERE id=?", (ident,))
        self.db.commit()
        sender = Mock(return_value='4')
        self.assertEqual(reminders.deliver_due(self.db, {}, clock=250, sender=sender), 0)
        self.assertEqual(reminders.deliver_due(self.db, {}, clock=261, sender=sender), 1)

    def test_time_requires_explicit_timezone(self):
        with self.assertRaises(ValueError):
            reminders.parse_due('2026-10-01T12:00:00')
        self.assertEqual(reminders.parse_due('2026-10-01T12:00:00-04:00'), reminders.parse_due('2026-10-01T16:00:00Z'))

    def test_second_worker_cannot_deliver_an_active_claim(self):
        reminders.add(self.db, 'Only once', 150, clock=100)
        second_sender = Mock(return_value='unexpected')
        def sender(_content, _ident):
            with sqlite3.connect(self.path) as second:
                self.assertEqual(reminders.deliver_due(second, {}, clock=200, sender=second_sender), 0)
            return '5'
        self.assertEqual(reminders.deliver_due(self.db, {}, clock=200, sender=sender), 1)
        second_sender.assert_not_called()

    def test_retry_limit_leaves_a_visible_failed_reminder(self):
        ident = reminders.add(self.db, 'Unavailable daemon', 150, clock=100)
        sender = Mock(side_effect=RuntimeError('Synthetic failure'))
        for clock in (200, 300, 500, 1000, 2000, 5000):
            reminders.deliver_due(self.db, {}, clock=clock, sender=sender)
        self.assertEqual(sender.call_count, 5)
        self.assertEqual(self.db.execute('SELECT status FROM reminders WHERE id=?', (ident,)).fetchone()[0], 'failed')
