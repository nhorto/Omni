import unittest
from unittest.mock import patch
import hypr


class WindowGeometryTests(unittest.TestCase):
    def test_focus_requires_active_window_to_match_exact_address(self):
        item = {'address': '0x1234', 'workspace': 2, 'title': 'Notes', 'class': 'notes'}
        with patch.object(hypr, 'window', return_value=item), patch.object(hypr, '_json', return_value={'address': '0x1234'}), patch.object(hypr.subprocess, 'run') as run:
            run.return_value.stdout = 'ok'
            self.assertEqual(hypr.focus_window('0x1234'), 'Focused Notes')
            self.assertIn('address:0x1234', run.call_args.args[0][-1])
        with patch.object(hypr, 'window', return_value=item), patch.object(hypr, '_json', return_value={'address': '0x5555'}), patch.object(hypr.subprocess, 'run') as run, patch.object(hypr.time, 'monotonic', side_effect=[0, 3]):
            run.return_value.stdout = 'ok'
            with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
                hypr.focus_window('0x1234')

    def test_scaled_rotated_monitor_respects_origin_and_reserved_panels(self):
        monitor = dict(width=3840, height=2160, scale=2, transform=1, x=-1080, y=100, reserved=[0, 40, 0, 0])
        x, y, width, height = hypr.placement_geometry(monitor, 'bottom-right')
        self.assertGreaterEqual(x, -1080)
        self.assertGreaterEqual(y, 140)
        self.assertLessEqual(x + width, 0)
        self.assertLessEqual(y + height, 2020)
        left = hypr.placement_geometry(monitor, 'bottom-left')
        self.assertLess(left[0] + left[2], x)
        self.assertEqual(left[1], y)

    def test_placement_rejects_unverified_size(self):
        item = dict(monitor=1, floating=True, at=[12, 12], size=[900, 900], title='test', **{'class':'test'})
        monitor = dict(id=1, name='test', width=1920, height=1080, x=0, y=0)
        with patch.object(hypr, 'window', return_value=item), patch.object(hypr, '_json', return_value=[monitor]), patch.object(hypr.subprocess, 'run') as run, patch.object(hypr.time, 'monotonic', side_effect=[0, 3]):
            run.return_value.stdout = 'ok'
            with self.assertRaisesRegex(RuntimeError, 'size could not be verified'):
                hypr.place_window('0x1234', 'left')
