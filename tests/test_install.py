import subprocess
import tempfile
import unittest
from pathlib import Path

import install


class InstallationTests(unittest.TestCase):
    def test_launchers_quote_checkout_and_backup_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / 'home'
            root = Path(folder) / "checkout with 'quotes'"
            root.mkdir()
            (root / 'integration').symlink_to(install.ROOT / 'integration', target_is_directory=True)
            for script in ('omi_app.py', 'voice_bridge.py'):
                (root / script).write_text('import sys\nprint(sys.argv[1])\n')
            install.install(root, home)
            launcher = home / '.local/bin/omi'
            self.assertEqual(subprocess.check_output([str(launcher), 'test argument'], text=True).strip(), 'test argument')
            self.assertEqual(install.install(root, home), [])
            launcher.write_text('old custom launcher')
            install.install(root, home)
            backups = list((home / '.local/share/omi/integration-backups').rglob('omi'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), 'old custom launcher')
            self.assertNotIn('@OMI_LAUNCHER@', (home / '.local/share/applications/omi.desktop').read_text())


if __name__ == '__main__':
    unittest.main()
