import json
from pathlib import Path
import shutil
import tempfile
import unittest

from efir.project import EfirError, Project, frame_count
from efir.render import export_srt, subtitles


class ProjectTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.project = Project.create(self.directory / "Монтаж", "Ночной выпуск")
        self.picture = self.directory / "Первый кадр.PNG"
        self.picture.write_bytes(b"an opaque test asset")

    def scene(self, title="Кадр", frames=25, caption=""):
        return self.project.add(title=title, visual=self.project.import_media(self.picture),
                                frames=frames, caption=caption)

    def test_import_deduplication_and_portability(self):
        first = self.scene()
        second = self.scene()
        self.assertEqual(first.visual, second.visual)
        self.assertEqual(len(list((self.project.root / "assets").iterdir())), 1)
        self.project.save()
        moved = self.directory / "Копия"
        shutil.copytree(self.project.root, moved)
        self.picture.unlink()
        reopened = Project.open(moved)
        self.assertEqual(reopened.scenes, [first, second])
        self.assertEqual(reopened.asset(first.visual).read_bytes(), b"an opaque test asset")

    def test_stale_save_and_creation_never_overwrite(self):
        another = Project.open(self.project.root)
        first = self.scene()
        self.project.save()
        with self.assertRaisesRegex(EfirError, "изменился"):
            another.save()
        with self.assertRaises(EfirError):
            Project.create(self.project.root, "Другое название")
        self.assertEqual(Project.open(self.project.root).scenes, [first])

    def test_invalid_asset_references_and_modified_asset(self):
        scene = self.scene()
        self.project.save()
        (self.project.root / "assets" / scene.visual).write_bytes(b"changed")
        with self.assertRaisesRegex(EfirError, "изменено"):
            self.project.asset(scene.visual)
        self.assertEqual(self.project.import_media(self.picture), scene.visual)
        manifest = self.project.root / "project.json"
        data = json.loads(manifest.read_bytes())
        data["scenes"][0]["visual"] = "../outside.png"
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(EfirError, "ссылку"):
            Project.open(self.project.root)

    def test_image_cannot_request_video_trim_or_source_audio(self):
        scene = self.scene()
        for fields in ({"start_frame": 1}, {"source_audio": True}):
            with self.assertRaises(EfirError):
                self.project.update(scene.id, **fields)
        self.assertEqual(self.project.scenes, [scene])

    def test_subtitles_follow_scene_order_and_frame_clock(self):
        first = self.scene(frames=23, caption="Где утка?\n\nУ окна.")
        self.scene(frames=29)
        third = self.scene(frames=1, caption="Буквально: <кадр> & голос.")
        self.project.move(third.id, 2)
        text = subtitles(self.project.scenes)
        self.assertIn("00:00:00,000 --> 00:00:00,920\nГде утка?\nУ окна.", text)
        self.assertIn("00:00:00,920 --> 00:00:00,960\nБуквально: &lt;кадр&gt; &amp; голос.", text)
        self.assertEqual(self.project.scenes[0].id, first.id)
        path = self.directory / "subtitles.srt"
        export_srt(self.project, path)
        with self.assertRaisesRegex(EfirError, "существует"):
            export_srt(self.project, path)
        self.assertEqual(path.read_bytes(), text.encode())

    def test_frame_duration_ranges(self):
        self.assertEqual(frame_count("0.02"), 1)
        self.assertEqual(frame_count("2.4"), 60)
        self.assertEqual(frame_count("0", zero=True), 0)
        for value in ("-1", "NaN", "Infinity", "601", "0.001", "invalid"):
            with self.subTest(value=value), self.assertRaises(EfirError):
                frame_count(value)
