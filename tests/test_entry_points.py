import gc
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from efir.project import Project
from test_render import FFMPEG


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Эфир CLI ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.project = self.directory / "Выпуск"

    def command(self, *args):
        call = subprocess.run([sys.executable, "-X", "utf8", "-m", "efir", *map(str, args)],
                              encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(call.returncode, 0, call.stderr)
        return call.stdout.strip()

    def test_cli_init_edit_move_remove_list_and_srt(self):
        picture = self.directory / "План.PNG"
        picture.write_bytes(b"Raw bytes for import-only CLI test")
        self.command("init", self.project, "--title", "Вечерний эфир")
        one = self.command("add", self.project, "--title", "Первый", "--visual", picture, "--duration", "1")
        two = self.command("add", self.project, "--title", "Второй", "--visual", picture,
                           "--duration", "0.52", "--caption", "Начинаем эфир")
        self.command("edit", self.project, two, "--title", "Новый план", "--notes", "Модель и референс")
        self.command("move", self.project, two, "1")
        listing = self.command("list", self.project)
        self.assertLess(listing.index(two), listing.index(one))
        self.assertIn("Новый план", listing)
        self.command("remove", self.project, one)
        self.assertEqual(len(Project.open(self.project).scenes), 1)
        output = self.directory / "Субтитры.srt"
        self.command("srt", self.project, output)
        self.assertIn("00:00:00,000 --> 00:00:00,520\nНачинаем эфир", output.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.environ.get("EFIR_TEST_DESKTOP") == "1" and FFMPEG,
                         "Set EFIR_TEST_DESKTOP=1 on a machine with Tk and FFmpeg")
    def test_tk_window_select_move_and_export_in_background(self):
        import tkinter
        from efir.desktop import Studio

        window = tkinter.Tk()
        window.withdraw()
        self.addCleanup(gc.collect)
        self.addCleanup(window.destroy)
        studio = Studio(window)
        project = Project.create(self.project, "Рабочее окно")
        picture = self.directory / "Изображение.png"
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=orange:s=96x64",
                        "-frames:v", "1", "-threads:v", "1", str(picture)], check=True, timeout=30)
        visual = project.import_media(picture)
        project.add(title="Начало", visual=visual, frames=5)
        second = project.add(title="Вторая сцена", visual=visual, frames=4, caption="В эфире")
        project.save()
        studio.open(project.root)
        studio.scenes.selection_set(second.id)
        window.update()
        studio.up_button.invoke()
        self.assertEqual(Project.open(project.root).scenes[0].id, second.id)
        destination = self.directory / "Выпуск с субтитрами.mp4"
        with patch.object(studio, "choose_output", return_value=destination), patch.object(studio, "error") as error:
            studio.export_button.invoke()
            deadline = time.monotonic() + 20
            while studio.cancel is not None and time.monotonic() < deadline:
                window.update()
                time.sleep(0.01)
            error.assert_not_called()
        self.assertIsNone(studio.cancel)
        self.assertEqual(studio.last_output, destination.resolve())
        self.assertGreater(destination.stat().st_size, 0)
