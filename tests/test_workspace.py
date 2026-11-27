"""Exercise chat, revision and queue controls in the actual Tk window."""

import gc
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from efir.jobs import Queue
from efir.project import EfirError, Project
from efir.store import StudioProject
from test_studio import VoiceFixture, wav


@unittest.skipUnless(os.environ.get("EFIR_TEST_DESKTOP") == "1", "Tk display required")
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk

        from efir.workspace import Workspace

        folder = tempfile.TemporaryDirectory(prefix="Эфир Интерфейс ")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.window = tk.Tk()
        self.window.withdraw()
        self.addCleanup(gc.collect)
        self.addCleanup(self.cleanup_window)
        self.studio = Workspace(self.window)
        self.studio.error = Mock()
        self.project = StudioProject.create(self.root / "Эфир", "Проект")
        self.studio.open(self.project.root)

    def cleanup_window(self):
        from tkinter import TclError

        try:
            for callback in self.window.tk.call("after", "info"):
                self.window.after_cancel(callback)
            self.window.destroy()
        except TclError:
            pass

    def pump(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.window.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail(f"Timed out. Workspace status: {self.studio.status.get()}")

    def test_chat_confirmation_restore_and_reject(self):
        studio = self.studio
        engine = Mock(model="fake")
        engine.plan.side_effect = lambda project, _: {
            "base_revision": project.revision,
            "operations": [{"op": "add", "title": "Студия", "frames": 25, "speech": "Начинаем."}],
        }
        studio.prompt.insert("1.0", "Добавь первую сцену")
        with patch("efir.workspace.Ollama", return_value=engine):
            studio.ask_button.invoke()
            studio.start_work(Mock())
            self.assertIn("Дождитесь", studio.status.get())
            self.pump(lambda: not studio.busy)
            self.assertIn("Добавить сцену", studio.conversation.get("1.0", "end"))
            self.assertFalse(studio.project.scenes)
            studio.apply_button.invoke()
            self.pump(lambda: not studio.busy)
            self.assertEqual(len(studio.project.scenes), 1)
            studio.prompt.insert("1.0", "Ещё одна сцена")
            studio.ask_button.invoke()
            self.pump(lambda: not studio.busy)
            studio.reject_button.invoke()
            self.pump(lambda: not studio.busy)
            self.assertIsNone(studio.pending())
            self.assertEqual(len(studio.project.scenes), 1)
        studio.revisions.selection_set(1)
        studio.restore()
        self.assertFalse(studio.project.scenes)
        self.assertEqual(studio.project.revision, 3)
        studio.error.assert_not_called()

    def test_import_voice_playback_tasks_and_export_failure(self):
        studio = self.studio
        manually = self.project.add(title="Реплика", visual=None, frames=25, speech="Эфир.")
        self.project.save()
        studio.open(self.project.root)
        studio.scenes.selection_set(manually.id)
        audio = self.root / "bed.wav"
        wav(audio)
        with patch("efir.workspace.filedialog.askopenfilename", return_value=str(audio)):
            studio.background()
            self.pump(lambda: not studio.busy)
        self.assertIsNotNone(studio.project.background)
        fixture = VoiceFixture()
        with patch("efir.workspace.Piper.configured", return_value=fixture):
            studio.voice()
            self.pump(lambda: not studio.busy and bool(studio.project.scenes[0].voice))
        self.assertEqual(fixture.calls, ["Эфир."])
        task = Queue(studio.project).jobs()[0]
        studio.tasks.selection_set(task["id"])
        with patch.object(studio, "open_media") as play:
            studio.listen()
            studio.task_action("open")
            self.assertEqual(play.call_count, 2)
        studio.render()
        self.pump(lambda: any(row["state"] == "failed" for row in studio.task_rows.values()))
        failure = Queue(studio.project).jobs()[0]
        studio.tasks.selection_set(failure["id"])
        studio.task_action("open")
        studio.task_action("retry")
        studio.task_action("cancel")
        self.assertEqual(Queue(studio.project).jobs()[0]["state"], "cancelled")
        studio.error.assert_not_called()

    def test_migration_portable_and_unavailable_ollama(self):
        studio = self.studio
        legacy = Project.create(self.root / "Legacy", "Старый проект")
        self.studio.open(legacy.root)
        self.assertIn("disabled", studio.ask_button.state())
        studio.voice()
        studio.error.assert_called_once()
        with patch("efir.workspace.filedialog.askdirectory", side_effect=[str(legacy.root), str(self.root)]), patch(
            "efir.workspace.simpledialog.askstring", return_value="Миграция",
        ):
            studio.migrate_project()
            self.pump(lambda: isinstance(studio.project, StudioProject) and studio.project.root.name == "Миграция")
        with patch("efir.workspace.filedialog.askdirectory", return_value=str(self.root)), patch(
            "efir.workspace.simpledialog.askstring", return_value="Копия",
        ):
            studio.backup()
            self.pump(lambda: not studio.busy)
        self.assertTrue((self.root / "Копия" / "project.sqlite").exists())
        studio.library.selection_set(0)
        studio.open_recent(None)
        self.assertEqual(studio.project.root, self.project.root)
        studio.prompt.insert("1.0", "Эфир")
        with patch("efir.workspace.propose", side_effect=EfirError("Ollama недоступен")):
            studio.ask()
            self.pump(lambda: not studio.busy)
        self.assertIn("недоступен", studio.status.get())

    def test_dialog_can_edit_voice_script_without_visual(self):
        from tkinter import ttk

        from efir.desktop import SceneDialog

        with patch("efir.desktop.simpledialog.Dialog.__init__", return_value=None):
            dialog = SceneDialog(self.window, self.project)
        with patch.object(dialog, "_row"):
            dialog.body(ttk.Frame(self.window))
            dialog.title_field.set("План")
            dialog.speech.insert("1.0", "Текст озвучки.")
            self.assertTrue(dialog.validate())
        self.assertIsNone(dialog.result["visual"])
        self.assertEqual(dialog.result["speech"], "Текст озвучки.")

    def test_cancelled_dialogs_and_legacy_controls(self):
        studio = self.studio
        with patch("efir.workspace.filedialog.askdirectory", return_value=""), patch(
            "efir.workspace.filedialog.askopenfilename", return_value="",
        ):
            studio.migrate_project()
            studio.backup()
            studio.background()
        with patch("efir.workspace.filedialog.askdirectory", return_value=str(self.root)), patch(
            "efir.workspace.simpledialog.askstring", return_value="..",
        ):
            studio.migrate_project()
            studio.backup()
        self.assertFalse(studio.busy)
        studio.open_recent(None)
        studio.restore()
        studio.voice()
        studio.listen()
        studio.task_action("open")
        studio.resolve_plan(True)
        studio.error.assert_not_called()
        legacy = Project.create(self.root / "Old", "Оригинал")
        studio.open(legacy.root)
        studio.backup()
        studio.background()
        studio.task_action("retry")
        self.assertEqual(studio.error.call_count, 3)
        with patch.object(studio, "choose_output", return_value=None) as choose:
            studio.render()
        choose.assert_called_once_with(".mp4")

    def test_switch_project_while_background_operation_is_running(self):
        from threading import Event

        studio = self.studio
        other = StudioProject.create(self.root / "Другой", "Другой эфир")
        ready = Event()
        studio.start_work(ready.wait)
        try:
            studio.open(other.root)
            self.assertEqual(studio.project.root, other.root)
        finally:
            ready.set()
        self.pump(lambda: not studio.busy)
        self.assertEqual(studio.project.root, other.root)
        with patch.object(studio.window, "destroy") as destroy:
            studio.start_work(lambda: None)
            studio.close()
            self.assertTrue(studio.closing)
            self.pump(lambda: bool(destroy.call_count))
            destroy.assert_called_once()

    def test_missing_voice_engine_fails_only_voice_job(self):
        studio = self.studio
        scene = self.project.add(title="Без озвучки", frames=10, visual=None, speech="Вы в эфире")
        self.project.save()
        studio.open(self.project.root)
        queue = Queue(studio.project)
        task = queue.enqueue("voice", scene_id=scene.id, recipe=VoiceFixture.recipe)
        with patch("efir.workspace.Piper.configured", side_effect=EfirError("Piper missing")):
            self.pump(lambda: studio.task_rows.get(task, {}).get("state") == "failed")
        self.assertIsNone(studio.project.scenes[0].voice)
        studio.scenes.selection_set(scene.id)
        with patch.object(studio, "open_media") as play:
            studio.listen()
        play.assert_not_called()
