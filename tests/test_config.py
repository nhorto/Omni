import tempfile
import unittest
from pathlib import Path

from omni.config import load_settings, save_setting


class ConfigTest(unittest.TestCase):
    def test_save_setting_round_trips_and_none_restores_default(self):
        path = Path(tempfile.mkdtemp()) / "config.toml"
        path.write_text('# mine\nmic_target = "yealink"\n')
        save_setting("model", "gpt-6-luna", path)
        save_setting("speech", False, path)
        save_setting("elevenlabs_voice_id", 'a"b', path)
        settings = load_settings(path)
        self.assertEqual((settings.model, settings.speech, settings.elevenlabs_voice_id), ("gpt-6-luna", False, 'a"b'))
        self.assertEqual(settings.extra["mic_target"], "yealink")
        save_setting("model", None, path)
        self.assertIsNone(load_settings(path).model)
        self.assertIn("# mine", path.read_text())


if __name__ == "__main__":
    unittest.main()
