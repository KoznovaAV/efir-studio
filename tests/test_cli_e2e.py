"""Black-box CLI workflow. This file also runs directly inside the image."""

from array import array
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class CliEndToEnd(unittest.TestCase):
    def setUp(self):
        self.ffmpeg = shutil.which(os.environ.get("EFIR_FFMPEG") or "ffmpeg")
        self.assertIsNotNone(self.ffmpeg, "Install FFmpeg with libx264/AAC to run E2E")
        scratch = tempfile.TemporaryDirectory(prefix="Эфир E2E ", dir=os.environ.get("EFIR_E2E_TEMP"))
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def execute(self, *args):
        result = subprocess.run([str(arg) for arg in args], capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return result.stdout

    def ff(self, *args):
        return self.execute(self.ffmpeg, "-v", "error", "-nostdin", *args)

    def cli(self, *args):
        return self.execute(sys.executable, "-X", "utf8", "-m", "efir", *args).decode("utf-8")

    def assets(self):
        image, video, voice = (self.root / name for name in ("Кадр.png", "Движение.mp4", "Голос.wav"))
        self.ff("-f", "lavfi", "-i", "color=blue:s=96x96", "-frames:v", 1, "-threads:v", 1, image)
        self.ff("-f", "lavfi", "-i", "testsrc2=size=128x96:rate=25:duration=0.84",
                "-c:v", "libx264", video)
        self.ff("-f", "lavfi", "-i", "sine=frequency=660:duration=0.60", voice)
        return image, video, voice

    def verify(self, output, frame_count, second_end):
        self.assertGreater(output.stat().st_size, 1000)
        data = self.ff("-i", output, "-map", "0:v:0", "-f", "framecrc", "-")
        self.assertIn(b"#dimensions 0: 540x960", data)
        self.assertIn(b"#tb 0: 1/25", data)
        frames = [line.split(b",") for line in data.splitlines() if line[:1].isdigit()]
        self.assertEqual([int(line[2]) for line in frames], list(range(frame_count)))
        self.assertGreater(len({line[-1] for line in frames[10:-5]}), 2)
        raw_audio = self.ff("-i", output, "-map", "0:a:0", "-f", "s16le", "-")
        self.assertLess(abs(len(raw_audio) // 4 - frame_count * 1920), 1024)
        audio = array("h")
        audio.frombytes(raw_audio)
        if sys.byteorder == "big":
            audio.byteswap()
        self.assertLess(max(map(abs, audio[:18000])), 50)
        self.assertGreater(max(map(abs, audio[48000:62000])), 500)
        captions = self.ff("-i", output, "-map", "0:s:0", "-f", "srt", "-").decode("utf-8")
        self.assertIn("00:00:00,000 --> 00:00:00,400\nНачинаем эфир.", captions)
        self.assertIn(f"00:00:00,400 --> {second_end}\nДвижение и звук.", captions)

    def test_create_export_modify_and_reuse_scenes(self):
        image, video, voice = self.assets()
        project, output = self.root / "Выпуск", self.root / "Эфир.mp4"
        self.cli("init", project, "--title", "Ночной эфир")
        self.cli("add", project, "--title", "Титр", "--visual", image,
                 "--duration", "0.40", "--caption", "Начинаем эфир.")
        changed = self.cli("add", project, "--title", "Клип", "--visual", video,
                           "--duration", "0.60", "--start", "0.04", "--voice", voice,
                           "--caption", "Движение и звук.").strip()
        self.cli("add", project, "--title", "Финал", "--visual", image, "--duration", "0.20")
        for source in (image, video, voice):
            source.unlink()
        self.assertIn("Кодирование: 3; из кэша: 0", self.cli("export", project, output, "--draft"))
        self.verify(output, 30, "00:00:01,000")
        initial = output.read_bytes()
        self.cli("edit", project, changed, "--duration", "0.48")
        self.assertIn("Кодирование: 1; из кэша: 2",
                      self.cli("export", project, output, "--draft", "--overwrite"))
        self.assertNotEqual(output.read_bytes(), initial)
        self.verify(output, 27, "00:00:00,880")
        srt = self.root / "Субтитры.srt"
        self.cli("srt", project, srt)
        self.assertIn("00:00:00,400 --> 00:00:00,880", srt.read_text(encoding="utf-8"))

    def test_sqlite_migration_queued_export_and_portable_copy(self):
        image, video, voice = self.assets()
        legacy, studio, copied = (self.root / name for name in ("Старый", "Студия", "Перенос"))
        self.cli("init", legacy, "--title", "В эфире")
        self.cli("add", legacy, "--title", "Титр", "--visual", image, "--duration", "0.40",
                 "--caption", "Начинаем эфир.")
        scene = self.cli("add", legacy, "--title", "Студия", "--visual", video, "--duration", "0.60",
                         "--caption", "Движение и звук.", "--voice", voice).strip()
        self.cli("migrate", legacy, studio)
        self.assertTrue((legacy / "project.json").is_file())
        self.assertFalse((studio / "project.json").exists())
        self.cli("edit", studio, scene, "--speech", "Готово к записи.")
        job = self.cli("queue-export", studio, "--draft").strip()
        self.assertEqual(self.cli("work", studio).strip(), job)
        complete = json.loads(self.cli("jobs", studio))
        self.assertEqual(complete["state"], "succeeded")
        result = json.loads(complete["result"])
        self.assertEqual(result["encoded"], 2)
        output = studio / result["file"]
        self.verify(output, 25, "00:00:01,000")

        self.cli("background", studio, voice)
        newer_job = self.cli("queue-export", studio, "--draft").strip()
        self.assertNotEqual(newer_job, job)
        self.cli("work", studio)
        updated = json.loads(self.cli("jobs", studio).splitlines()[0])
        newer = json.loads(updated["result"])
        self.assertEqual(newer["reused"], 2)
        audio = self.ff("-i", studio / newer["file"], "-map", "0:a:0", "-f", "s16le", "-")
        self.assertNotEqual(audio[:19000], self.ff("-i", output, "-map", "0:a:0", "-f", "s16le", "-")[:19000])
        self.cli("portable", studio, copied)
        self.assertEqual((copied / result["file"]).read_bytes(), output.read_bytes())
        self.assertIn(scene, self.cli("list", copied))
        self.assertIn("Фоновая дорожка", self.cli("history", copied))
        self.cli("restore", copied, "2")
        self.assertIn("Восстановление ревизии 2", self.cli("history", copied))
        self.cli("background", copied)

    def test_sqlite_command_errors_and_task_actions(self):
        studio = self.root / "Новый"
        self.cli("init", studio, "--sqlite", "--title", "Новый")
        failed = self.cli("queue-export", studio).strip()
        self.cli("work", studio)
        self.assertEqual(json.loads(self.cli("jobs", studio))["state"], "failed")
        self.cli("retry", studio, failed)
        self.cli("cancel", studio, failed)
        self.assertEqual(json.loads(self.cli("jobs", studio))["state"], "cancelled")
        self.assertIn("Нет доступных", self.cli("work", studio))


if __name__ == "__main__":
    unittest.main()
