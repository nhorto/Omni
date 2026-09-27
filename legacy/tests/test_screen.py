import subprocess
import unittest
from unittest.mock import patch

import screen


class ScreenReadTests(unittest.TestCase):
    def test_ocr_reads_png_from_memory_and_returns_text(self):
        captures = [subprocess.CompletedProcess([], 0, b"PNG_BYTES", b""), subprocess.CompletedProcess([], 0, b"Visible heading\n", b"")]
        with patch.object(screen.shutil, "which", return_value="/usr/bin/tool"), patch.object(screen.subprocess, "run", side_effect=captures) as run:
            self.assertEqual(screen.read_text(), "Visible heading")
            self.assertEqual(run.call_args_list[1].kwargs["input"], b"PNG_BYTES")

    def test_capture_failure_is_reported_without_storing_image(self):
        with patch.object(screen.shutil, "which", return_value="/usr/bin/tool"), patch.object(screen.subprocess, "run", side_effect=subprocess.TimeoutExpired("grim", 15)):
            with self.assertRaisesRegex(RuntimeError, "Screen OCR failed"):
                screen.read_text()
