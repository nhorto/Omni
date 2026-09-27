import unittest
from unittest.mock import Mock, patch

import desktop


class DesktopFillTests(unittest.TestCase):
    def test_click_target_rejects_stale_or_extended_label(self):
        node = Mock()
        with patch.object(desktop, 'controls', return_value=[('Save', 'button', node)]):
            with self.assertRaisesRegex(ValueError, 'not unique'):
                desktop.resolve('Notes', 'Save previous version')

    def test_exact_unique_field_is_filled_and_read_back(self):
        editable = Mock()
        editable.set_text_contents.return_value = True
        readable = Mock()
        readable.get_text.return_value = 'Hello'
        node = Mock()
        node.get_editable_text_iface.return_value = editable
        node.get_text_iface.return_value = readable
        with patch.object(desktop, 'editable_fields', return_value=[('Search', node)]):
            self.assertEqual(desktop.fill('Notes', 'Search', 'Hello'), 'Filled Search in Notes')
            editable.set_text_contents.assert_called_once_with('Hello')
            readable.get_text.assert_called_once_with(0, -1)

    def test_ambiguous_field_or_unverified_text_fails(self):
        node = Mock()
        node.get_editable_text_iface.return_value.set_text_contents.return_value = True
        node.get_text_iface.return_value.get_text.return_value = 'old value'
        with patch.object(desktop, 'editable_fields', return_value=[('Search', node), ('Search', node)]):
            with self.assertRaisesRegex(ValueError, 'not unique'):
                desktop.fill('Notes', 'Search', 'new value')
        with patch.object(desktop, 'editable_fields', return_value=[('Search', node)]):
            with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
                desktop.fill('Notes', 'Search', 'new value')
