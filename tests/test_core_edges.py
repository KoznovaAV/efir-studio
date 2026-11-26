"""Input validation, concurrency and process failure contracts."""

from dataclasses import replace
import json
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from efir.__main__ import main
from efir.project import EfirError, Project, comparable_path, digest_file, project_lock
from efir.render import Renderer, export_srt


class CoreEdges(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="Проверка ядра ")
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.project = Project.create(self.root / "Проект", "Эфир")
        self.source = self.root / "visual.jpg"
        self.source.write_bytes(b"Opaque input for project validation")
        self.visual = self.project.import_media(self.source)

    def test_scene_contract(self):
        scene = self.project.add(title="Вступление", visual=self.visual, frames=20)
        for fields, reason in (
            ({"id": 42}, "идентификатор"),
            ({"id": "bad"}, "идентификатор"),
            ({"title": " "}, "название"),
            ({"caption": "\x00"}, "NUL"),
            ({"notes": 1}, "название"),
            ({"frames": True}, "Длительность"),
            ({"frames": 0}, "Длительность"),
            ({"frames": 15001}, "Длительность"),
            ({"start_frame": False}, "позиция"),
            ({"start_frame": -1}, "позиция"),
            ({"fit": "stretch"}, "кадрирования"),
            ({"source_audio": 1}, "кадрирования"),
            ({"visual": None}, "ссылку"),
            ({"visual": "a" * 64 + ".wav"}, "ссылку"),
            ({"voice": self.visual}, "ссылку"),
        ):
            with self.subTest(fields=fields), self.assertRaisesRegex(EfirError, reason):
                replace(scene, **fields).validate()
        self.project.scenes.append(scene)
        with self.assertRaisesRegex(EfirError, "повторяются"):
            self.project.save()

    def test_manifest_validation(self):
        manifest = self.project.root / "project.json"
        for body, reason in (
            (b"}", "поврежд"),
            (b'{"version":2}', "версия"),
            (b'{"version":true}', "версия"),
            (b'{"version":1,"scenes":{}}', "поврежд"),
            (b'{"version":1,"scenes":[]}', "поврежд"),
        ):
            with self.subTest(body=body), self.assertRaisesRegex(EfirError, reason):
                manifest.write_bytes(body)
                Project.open(self.project.root)
        with self.assertRaisesRegex(EfirError, "нет project.json"):
            Project.open(self.root / "Не существует")
        for title, aspect, reason in ((None, "9:16", "название"), (" ", "9:16", "название"),
                                      ("a\x00b", "9:16", "название"), ("x", [], "форматы"),
                                      ("x", "2:3", "форматы")):
            with self.subTest(title=title, aspect=aspect), self.assertRaisesRegex(EfirError, reason):
                Project.create(self.root / "Несохранённый", title, aspect)
        self.assertFalse((self.root / "Несохранённый").exists())

    def test_project_paths_reject_parent_escape(self):
        with self.assertRaisesRegex(EfirError, "за пределы"):
            self.project.inside("..", "outside")

    def test_manifest_rejects_symlink_outside_project(self):
        outside = self.root / "elsewhere.json"
        outside.write_text(json.dumps({"version": 1, "title": "Other", "aspect": "9:16", "scenes": []}))
        link = self.root / "Привязка"
        link.mkdir()
        try:
            (link / "project.json").symlink_to(outside)
        except OSError:
            self.skipTest("This Windows context cannot create symlinks")
        with self.assertRaisesRegex(EfirError, "за пределы"):
            Project.open(link)

    def test_extended_windows_path_forms_compare_without_changing_containment(self):
        local = PureWindowsPath(r"D:\Эфир")
        network = PureWindowsPath(r"\\server\share\Эфир")
        for root, candidate, inside in (
            (local, r"\\?\D:\Эфир\.cache\new.mkv", True),
            (local, r"\\?\D:\Эфир другой\new.mkv", False),
            (local, r"\\?\E:\Эфир\new.mkv", False),
            (network, r"\\?\UNC\SERVER\share\Эфир\project.json", True),
            (network, r"\\?\UNC\other\share\Эфир\project.json", False),
        ):
            with self.subTest(candidate=candidate):
                actual = comparable_path(PureWindowsPath(candidate))
                self.assertEqual(actual.is_relative_to(comparable_path(root)), inside)

    def test_media_and_export_validation(self):
        invalid = self.root / "bad.gif"
        invalid.write_bytes(b"GIF")
        empty = self.root / "empty.mp4"
        empty.touch()
        for source, message in ((invalid, "Выберите"), (self.root / "absent.png", "Выберите"), (empty, "пуст")):
            with self.subTest(source=source), self.assertRaisesRegex(EfirError, message):
                self.project.import_media(source)
        for path in (self.root / "else.mp4", self.root / "missing" / "x.srt",
                     self.project.root / "assets" / "x.srt", self.project.root / ".cache" / "x.srt"):
            with self.subTest(path=path), self.assertRaisesRegex(EfirError, "существующей папке"):
                self.project.output(path, ".srt")
        with self.assertRaisesRegex(EfirError, "Добавьте субтитры"):
            export_srt(self.project, self.root / "no.srt")
        scene = self.project.add(title="A", visual=self.visual, frames=1)
        with self.assertRaisesRegex(EfirError, "позиция"):
            self.project.move(scene.id, 0)
        with self.assertRaisesRegex(EfirError, "не найдена"):
            self.project.update("wrong", title="B")
        self.assertEqual(self.project.scenes, [scene])

    def test_os_lock_rejects_an_independent_process_and_releases(self):
        attempt = (
            "import sys; from efir.project import Project; "
            "project = Project.open(sys.argv[1]); project.title = sys.argv[2]; project.save()"
        )
        args = [sys.executable, "-X", "utf8", "-c", attempt, str(self.project.root), "Новое название"]
        with project_lock(self.project.root / ".project.lock"):
            contender = subprocess.run(args, capture_output=True, encoding="utf-8", timeout=15)
            self.assertNotEqual(contender.returncode, 0)
            self.assertIn("сохраняется в другом окне", contender.stderr)
            self.assertEqual(Project.open(self.project.root).title, "Эфир")
        subprocess.run(args, check=True, timeout=15)
        self.assertEqual(Project.open(self.project.root).title, "Новое название")

    def test_cli_interrupt_and_unknown_project(self):
        with patch("efir.__main__.run", side_effect=KeyboardInterrupt):
            self.assertEqual(main(["list", str(self.root)]), 130)
        with patch("sys.stderr") as stderr:
            self.assertEqual(main(["list", str(self.root / "missing")]), 1)
            self.assertIn("нет project.json", "".join(str(c.args[0]) for c in stderr.write.call_args_list))


class RendererEdges(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)

    def test_unavailable_binary_and_existing_or_empty_output(self):
        unavailable = self.root / "missing"
        with self.assertRaisesRegex(EfirError, "Нужен FFmpeg"):
            Renderer(str(unavailable))
        renderer = Renderer(sys.executable)
        project = Project.create(self.root / "project", "Project")
        target = self.root / "movie.mp4"
        with self.assertRaisesRegex(EfirError, "хотя бы одну сцену"):
            renderer.export(project, target)
        target.write_bytes(b"previous")
        with self.assertRaisesRegex(EfirError, "уже существует"):
            renderer.export(project, target)
        self.assertEqual(target.read_bytes(), b"previous")

    def test_process_timeout_and_launch_error(self):
        renderer = Renderer(sys.executable)
        with self.assertRaisesRegex(EfirError, "превысил допустимое время"):
            renderer._run([], "Задача", timeout=0)
        renderer.ffmpeg = str(self.root / "missing")
        with self.assertRaisesRegex(EfirError, "не удалось запустить"):
            renderer._run([], "Задача", timeout=1)

    def test_racing_segment_publication_and_cache_validation(self):
        project = Project.create(self.root / "project", "Project")
        directory = project.root / ".cache" / "key"
        directory.mkdir(parents=True)
        (directory / "ignored.mkv").write_bytes(b"ignore names with no digest")
        renderer = Renderer(sys.executable)
        self.assertIsNone(renderer._cached_segment(project, directory))
        candidate = self.root / "source"
        candidate.write_bytes(b"good segment")
        segment = directory / (digest_file(candidate) + ".mkv")
        segment.write_bytes(b"good segment")
        renderer._publish_segment(candidate, segment)
        self.assertEqual(candidate.read_bytes(), b"good segment")
        self.assertEqual(renderer._cached_segment(project, directory), segment)
        segment.write_bytes(b"corrupt")
        self.assertIsNone(renderer._cached_segment(project, directory))
        renderer._publish_segment(candidate, segment)
        self.assertEqual(segment.read_bytes(), b"good segment")
