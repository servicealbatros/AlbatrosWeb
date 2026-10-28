"""Exercise the production worker entry point, including its DSM dependencies."""
import unittest
from datetime import datetime
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app
import dsm_pre_arrival
import pre_arrival_reminders as reminders


class PreArrivalWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_passes_existing_order_reader_to_real_tick_entry_point(self):
        now = datetime(2026, 10, 28, 17, tzinfo=reminders.ROMANIA)
        with patch.object(reminders, 'tick', AsyncMock(return_value={'sent': 0})) as tick:
            self.assertEqual(await app._pre_arrival_tick(now), {'sent': 0})
        args, kwargs = tick.await_args
        self.assertIs(args[2], now)
        self.assertIs(args[4], dsm_pre_arrival.seed)
        self.assertIs(kwargs['check_orders'], dsm_pre_arrival.existing_orders)
        with patch.object(app, 'import_dsm_schedule', AsyncMock(return_value={'appointment_ids': []})) as refresh:
            await kwargs['refresh'](now.date())
            refresh.assert_awaited_once_with(app.DB_PATH, now.date())

    def test_health_exposes_failure_and_clears_it_after_success(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            reminders.configure(root, True, '2026-10-28', ['B112TST', 'B113TST'])
            reminders.record_run(root, error='missing provider dependency')
            self.assertEqual(reminders.settings(root)['worker_health']['state'], 'error')
            reminders.record_run(root, {'sent': 2, 'failed': 0})
            state = reminders.settings(root)
            self.assertEqual(state['worker_health']['state'], 'running')
            self.assertEqual(state['worker_health']['error'], '')
            self.assertEqual(state['worker_health']['last_result']['sent'], 2)
            self.assertEqual(state['registrations'], ['B112TST', 'B113TST'])
