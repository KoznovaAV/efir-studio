"""Tk widget and view behaviour with deterministic dialog inputs."""

import gc
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from efir.project import EfirError, Project
from efir.render import RenderCancelled


@unittest.skipUnless(os.environ.get("EFIR_TEST_DESKTOP") == "1", "Tk or Xvfb is required")
class DesktopTests(unittest.TestCase):
    def setUp(self):
        import tkinter
        from efir import desktop

        self.tk, self.desktop = tkinter, desktop
        directory = tempfile.TemporaryDirectory(prefix="Окно EFIR ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.window = tkinter.Tk()
        self.window.withdraw()
        self.addCleanup(self.destroy_window)
        self.studio = desktop.Studio(self.window)
        self.errors = self.enterContext(patch.object(desktop.messagebox, "showerror"))
        self.project = Project.create(self.root / "Проект", "Эфир")
        self.image = self.root / "Кадр.png"
        self.clip = self.root / "Клип.mp4"
        self.voice = self.root / "Голос.wav"
        for source in (self.image, self.clip, self.voice):
            source.write_bytes(f"Sample for GUI: {source.name}".encode())

    def destroy_window(self):
        try:
            self.window.update_idletasks()
            self.window.destroy()
        except self.tk.TclError:
            pass
        self.errors.reset_mock()
        self.studio = None
        self.window = None
        # Tk objects must be finalized on their creating thread before later tests start workers.
        gc.collect()

    def populate(self):
        visual = self.project.import_media(self.clip)
        voice = self.project.import_media(self.voice, voice=True)
        first = self.project.add(title="Клип", visual=visual, frames=25, source_audio=True)
        second = self.project.add(title="Две дорожки", visual=visual, frames=15, source_audio=True,
                                  voice=voice, caption=" Субтитр\n ", notes="Описание")
        self.project.save()
        self.studio.open(self.project.root)
        self.studio.scenes.selection_set(second.id)
        self.studio.selection_changed()
        return first, second

    def make_dialog(self, scene=None):
        def initialize(dialog, parent, title):
            self.tk.Toplevel.__init__(dialog, parent)
            dialog.withdraw()
            dialog.title(title)
            dialog.body(self.tk.Frame(dialog))

        with patch.object(self.desktop.simpledialog.Dialog, "__init__", initialize):
            dialog = self.desktop.SceneDialog(self.window, self.project, scene)
        self.addCleanup(dialog.destroy)
        return dialog

    def wait_for_export(self):
        deadline = time.monotonic() + 5
        while self.studio.cancel is not None and time.monotonic() < deadline:
            self.window.update()
            time.sleep(0.01)
        self.assertIsNone(self.studio.cancel, "The worker did not signal completion")

    def test_new_project_cancel_invalid_format_and_success(self):
        with patch.object(self.desktop.filedialog, "askdirectory", return_value=""):
            self.studio.new_button.invoke()
        self.assertIsNone(self.studio.project)
        with patch.object(self.desktop.filedialog, "askdirectory", return_value=str(self.root)):
            for replies, message in (([None], None), (["bad/name"], "имени папки"),
                                     (["Другой", None], None), (["Другой", "2:3"], "Допустимые")):
                with self.subTest(replies=replies), patch.object(
                    self.desktop.simpledialog, "askstring", side_effect=replies
                ):
                    self.errors.reset_mock()
                    self.studio.new_button.invoke()
                    self.assertFalse((self.root / "Другой").exists())
                    if message:
                        self.assertIn(message, self.errors.call_args.args[1])
                    else:
                        self.errors.assert_not_called()
            with patch.object(self.desktop.simpledialog, "askstring", side_effect=["  Другой  ", " 1:1 "]):
                self.studio.new_button.invoke()
        self.assertEqual(self.studio.heading.get(), "Другой · 1:1")
        self.assertTrue(self.studio.export_button.instate(["disabled"]))
        self.assertEqual(Project.open(self.root / "Другой").aspect, "1:1")

    def test_open_cancel_bad_project_and_diagnostics(self):
        with patch.object(self.desktop.filedialog, "askdirectory", return_value=""):
            self.studio.open_button.invoke()
        self.errors.assert_not_called()
        with patch.object(self.desktop.filedialog, "askdirectory", return_value=str(self.root)):
            self.studio.open_button.invoke()
        self.assertIn("нет project.json", self.errors.call_args.args[1])
        with patch.object(self.desktop.filedialog, "askdirectory", return_value=str(self.project.root)):
            self.studio.open_button.invoke()
        self.assertEqual(self.studio.project.root, self.project.root)

    def test_scene_dialog_edit_and_image_audio_rules(self):
        _, second = self.populate()
        dialog = self.make_dialog(second)
        self.assertEqual(dialog.title_field.get(), second.title)
        self.assertEqual(dialog.voice_label.get(), "Дорожка проекта")
        self.assertTrue(dialog.validate())
        self.assertNotIn("visual", dialog.result)
        with patch.object(self.desktop.filedialog, "askopenfilename", return_value=""):
            dialog._choose(False)
        self.assertIsNone(dialog.visual)
        with patch.object(self.desktop.filedialog, "askopenfilename", return_value=str(self.image)):
            dialog._choose(False)
        self.assertEqual(dialog.start.get(), "0")
        self.assertFalse(dialog.source_audio.get())
        dialog.start.set("0,04")
        self.assertFalse(dialog.validate())
        self.assertIn("Для изображения", self.errors.call_args.args[1])
        dialog.start.set("0")
        dialog._remove_voice()
        self.assertTrue(dialog.validate())
        self.assertIsNone(dialog.result["voice"])
        self.assertTrue(dialog.result["visual"].endswith(".png"))

    def test_scene_dialog_new_voice_duration_and_errors(self):
        dialog = self.make_dialog()
        self.assertFalse(dialog.validate())
        dialog.title_field.set("   Планы   ")
        self.assertFalse(dialog.validate())
        for is_voice, source in ((False, self.clip), (True, self.voice)):
            with patch.object(self.desktop.filedialog, "askopenfilename", return_value=str(source)):
                dialog._choose(is_voice)
        self.assertEqual(dialog.visual_label.get(), self.clip.name)
        self.assertEqual(dialog.voice_label.get(), self.voice.name)
        dialog.duration.set("0.001")
        self.assertFalse(dialog.validate())
        dialog.duration.set("0,12")
        dialog.start.set("0,04")
        dialog.caption.insert("1.0", "Эфир")
        dialog.notes.insert("1.0", "Проверка")
        self.assertTrue(dialog.validate())
        self.assertEqual((dialog.result["title"], dialog.result["frames"], dialog.result["start_frame"]),
                         ("Планы", 3, 1))
        self.assertEqual(dialog.result["caption"], "Эфир")
        self.assertEqual(dialog.result["notes"], "Проверка")
        self.assertEqual(self.project.asset(dialog.result["voice"]).read_bytes(), self.voice.read_bytes())

    def test_edit_cancel_update_add_and_remove(self):
        first, second = self.populate()
        self.assertEqual(self.studio.scenes.item(first.id, "values")[-1], "Клип")
        self.assertEqual(self.studio.scenes.item(second.id, "values")[-1], "Клип + озвучка")
        self.assertIn("Субтитр · Описание", self.studio.description.get())
        with patch.object(self.desktop, "SceneDialog") as dialog:
            dialog.return_value.result = None
            self.studio.edit_button.invoke()
            self.assertEqual(Project.open(self.project.root).scenes[1], second)
            dialog.return_value.result = {"title": "Новый план"}
            self.studio.edit_button.invoke()
            self.assertEqual(Project.open(self.project.root).scenes[1].title, "Новый план")
            dialog.return_value.result = {"title": "Вставка", "visual": first.visual, "frames": 2}
            self.studio.add_button.invoke()
            self.assertEqual(Project.open(self.project.root).scenes[-1].title, "Вставка")
        with patch.object(self.desktop.messagebox, "askyesno", side_effect=[False, True]):
            self.studio.remove_button.invoke()
            self.assertEqual(len(self.studio.project.scenes), 3)
            self.studio.remove_button.invoke()
        self.assertEqual(len(Project.open(self.project.root).scenes), 2)

    def test_stale_gui_edit_rolls_back(self):
        first, second = self.populate()
        another = Project.open(self.project.root)
        another.update(first.id, title="Сохранено из другого окна")
        another.save()
        self.studio.up_button.invoke()
        self.assertIn("изменился", self.errors.call_args.args[1])
        self.assertEqual(self.studio.project.scenes, [first, second])
        self.assertEqual(Project.open(self.project.root).scenes[0].title, "Сохранено из другого окна")

    def test_srt_selection_and_invalid_output(self):
        self.populate()
        target = self.root / "sub.srt"
        with patch.object(self.desktop.filedialog, "asksaveasfilename",
                          side_effect=["", str(self.root / "bad.mp4"), str(target)]):
            self.studio.srt_button.invoke()
            self.errors.assert_not_called()
            self.studio.srt_button.invoke()
            self.assertIn("Выберите путь .srt", self.errors.call_args.args[1])
            self.studio.srt_button.invoke()
        self.assertIn("Субтитр", target.read_text(encoding="utf-8"))
        self.assertEqual(self.studio.status.get(), "Файл SRT сохранён.")

    def test_cancelled_save_dialog_and_failing_worker(self):
        self.populate()
        target = self.root / "failed.mp4"
        with patch.object(self.desktop.filedialog, "asksaveasfilename", side_effect=["", str(target)]):
            self.studio.export_button.invoke()
            self.assertIsNone(self.studio.cancel)
            with patch.object(self.desktop, "Renderer", side_effect=EfirError("FFmpeg отсутствует")):
                self.studio.export_button.invoke()
                self.wait_for_export()
        self.assertIn("FFmpeg отсутствует", self.errors.call_args.args[1])
        self.assertTrue(self.studio.export_button.instate(["!disabled"]))
        self.assertFalse(target.exists())

    def test_cancel_worker_and_close(self):
        self.populate()
        target = self.root / "cancelled.mp4"
        running = Event()

        def cancel_render(*args, **kwargs):
            running.set()
            self.assertTrue(self.studio.cancel.wait(3))
            raise RenderCancelled("Экспорт отменён.")

        with patch.object(self.desktop.filedialog, "asksaveasfilename", return_value=str(target)):
            with patch.object(self.desktop, "Renderer") as renderer:
                renderer.return_value.export.side_effect = cancel_render
                self.studio.export_button.invoke()
                self.assertTrue(running.wait(3))
                self.assertTrue(self.studio.edit_button.instate(["disabled"]))
                self.studio.edit_selected()
                self.studio.close()
                self.wait_for_export()
        self.assertEqual(self.studio.status.get(), "Экспорт отменён.")
        self.assertFalse(target.exists())
        self.studio.close()

    def test_open_visual_output_and_os_error(self):
        _, second = self.populate()
        visual = self.project.asset(second.visual)
        output = self.root / "published.mp4"
        opener = patch.object(self.desktop.os, "startfile") if os.name == "nt" else patch.object(
            self.desktop.subprocess, "Popen"
        )
        with opener as launch:
            self.studio.open_visual()
            self.studio.last_output = output
            self.studio.open_output()
            call_args = [call.args[0] for call in launch.call_args_list]
            opened = call_args if os.name == "nt" else [Path(args[-1]) for args in call_args]
            self.assertEqual(opened, [visual, output])
            launch.side_effect = OSError("Обработчик файла отсутствует")
            self.studio.open_output()
            self.assertIn("Обработчик", self.errors.call_args.args[1])
            visual.unlink()
            self.studio.open_visual()
            self.assertIn("Медиа отсутствует", self.errors.call_args.args[1])

    def test_launch_initial_project_and_tk_error(self):
        from efir.__main__ import main

        def process_startup():
            self.window.update()
            self.assertTrue(any("Эфир" in str(variable) for variable in headings))

        headings = []
        init = self.desktop.Studio.open

        def open_and_record(studio, path):
            init(studio, path)
            headings.append(studio.heading.get())

        with patch.object(self.desktop.tk, "Tk", return_value=self.window), patch.object(
            self.window, "mainloop", side_effect=process_startup
        ), patch.object(self.desktop.Studio, "open", open_and_record):
            self.assertEqual(main(["desktop", str(self.project.root)]), 0)
        with patch.object(self.desktop.tk, "Tk", side_effect=self.tk.TclError("no session")):
            with self.assertRaisesRegex(EfirError, "Не удалось открыть"):
                self.desktop.launch()
