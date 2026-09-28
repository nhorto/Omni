import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import install
from omni import config, doctor


class DropInTest(unittest.TestCase):
    def install_with(self, text: str) -> str | None:
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(config, "CONFIG", Path(folder)):
            (Path(folder) / "config.toml").write_text(text)
            return install.echo_cancel()

    def test_filled_from_config(self):
        conf = self.install_with('echo_cancel_mic = "mic.node"\necho_cancel_speaker = "speaker.node"\n')
        self.assertIn('target.object = "mic.node"', conf)
        self.assertIn('target.object = "speaker.node"', conf)
        self.assertNotIn("@", conf)
        for name in ("omni-echo-cancel-source", "omni-echo-cancel-sink", "aec/libspa-aec-webrtc"):
            self.assertIn(name, conf)

    def test_skipped_until_both_devices_are_set(self):
        self.assertIsNone(self.install_with('echo_cancel_mic = "mic.node"\n'))
        self.assertIsNone(self.install_with(""))


class DoctorTest(unittest.TestCase):
    def check(self, extra: dict, nodes: str) -> list:
        listing = subprocess.CompletedProcess([], 0, stdout=nodes)
        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/pw-cli"), \
             mock.patch.object(doctor.subprocess, "run", return_value=listing):
            return doctor._echo_cancel(config.Settings(extra=extra))

    def test_not_checked_for_plain_devices(self):
        self.assertEqual(self.check({"mic_target": "bluez_input.x"}, ""), [])

    def test_missing_nodes_fail(self):
        [(_, status, detail)] = self.check({"mic_target": "omni-echo-cancel-source"}, 'node.name = "other"')
        self.assertEqual(status, "fail")
        self.assertIn("omni-echo-cancel-source", detail)

    def test_both_present(self):
        nodes = 'node.name = "omni-echo-cancel-source"\nnode.name = "omni-echo-cancel-sink"\n'
        [(_, status, _)] = self.check({"mic_target": "omni-echo-cancel-source",
                                       "speaker_target": "omni-echo-cancel-sink"}, nodes)
        self.assertEqual(status, "ok")

    def test_half_setup_warns(self):
        [(_, status, _)] = self.check({"mic_target": "omni-echo-cancel-source"}, 'node.name = "omni-echo-cancel-source"')
        self.assertEqual(status, "warn")


if __name__ == "__main__":
    unittest.main()
