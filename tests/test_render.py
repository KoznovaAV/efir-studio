import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Event
import unittest

from efir.project import EfirError, Project
from efir.render import RenderCancelled, Renderer

FFMPEG = shutil.which(os.environ.get("EFIR_FFMPEG") or "ffmpeg")


@unittest.skipUnless(FFMPEG, "FFmpeg with libx264 is required")
class RenderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Эфир тест ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.project = Project.create(self.directory / "Проект с пробелом", "Тестовый выпуск")
        self.video = self.directory / "Движение.mp4"
        self.picture = self.directory / "Кадр.png"
        self.audio = self.directory / "Озвучка.wav"
        self.ffmpeg("-f", "lavfi", "-i", "testsrc2=size=144x96:rate=25:duration=0.84",
                    "-f", "lavfi", "-i", "sine=frequency=300:duration=0.84",
                    "-c:v", "libx264", "-c:a", "aac", str(self.video))
        self.ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=112x96", "-frames:v", "1",
                    "-threads:v", "1", str(self.picture))
        self.ffmpeg("-f", "lavfi", "-i", "sine=frequency=600:sample_rate=44100:duration=0.70",
                    str(self.audio))
        self.clip = self.project.import_media(self.video)
        self.still = self.project.import_media(self.picture)
        self.voice = self.project.import_media(self.audio, voice=True)

    def ffmpeg(self, *args):
        call = subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", *args],
                              capture_output=True, timeout=30)
        self.assertEqual(call.returncode, 0, call.stderr.decode(errors="replace"))
        return call.stdout

    def test_three_scene_mp4_and_selective_rerender(self):
        self.project.add(title="Движение", visual=self.clip, frames=13, source_audio=True,
                         start_frame=2, voice=self.voice, caption="В эфире.")
        target = self.project.add(title="Голос", visual=self.still, voice=self.voice,
                                  frames=9, caption="Вторая сцена.")
        self.project.add(title="Тишина", visual=self.still, frames=7, fit="cover")
        self.project.save()
        project = Project.open(self.project.root)
        output = self.directory / "Первый выпуск.mp4"
        renderer = Renderer(FFMPEG)
        result = renderer.export(project, output, draft=True)
        self.assertEqual((result.encoded, result.reused), (3, 0))
        raw = self.ffmpeg("-i", str(output), "-map", "0:v:0", "-f", "framecrc", "-")
        self.assertIn(b"#dimensions 0: 540x960", raw)
        frames = [line.split(b",") for line in raw.splitlines() if line[:1].isdigit()]
        self.assertEqual(len(frames), 29)
        self.assertGreater(len({line[-1] for line in frames[:13]}), 2)
        audio = self.ffmpeg("-i", str(output), "-map", "0:a:0", "-f", "s16le", "-")
        expected = 29 * 1920
        self.assertLess(abs(len(audio) // 4 - expected), 1024)
        self.assertGreater(max(audio[:48000]), 0)
        self.assertEqual(set(audio[-16000:]), {0})
        captions = self.ffmpeg("-i", str(output), "-map", "0:s:0", "-f", "srt", "-").decode()
        self.assertIn("В эфире.", captions)
        self.assertIn("00:00:00,520 --> 00:00:00,880", captions)

        project.update(target.id, frames=8)
        project.save()
        result = renderer.export(project, output, overwrite=True, draft=True)
        self.assertEqual((result.encoded, result.reused), (1, 2))
        project.update(target.id, caption="Правка субтитров.")
        project.move(target.id, 1)
        project.save()
        result = renderer.export(project, output, overwrite=True, draft=True)
        self.assertEqual((result.encoded, result.reused), (0, 3))
        captions = self.ffmpeg("-i", str(output), "-map", "0:s:0", "-f", "srt", "-").decode()
        self.assertIn("00:00:00,000 --> 00:00:00,320\nПравка субтитров.", captions)

    def test_failure_and_cancel_preserve_existing_output(self):
        video = self.directory / "bad.mp4"
        video.write_bytes(b"invalid video")
        self.project.add(title="Повреждённый клип", visual=self.project.import_media(video), frames=1)
        output = self.directory / "existing.mp4"
        output.write_bytes(b"existing output")
        with self.assertRaisesRegex(EfirError, "Повреждённый клип"):
            Renderer(FFMPEG).export(self.project, output, overwrite=True, draft=True)
        self.assertEqual(output.read_bytes(), b"existing output")
        cancel = Event()
        cancel.set()
        with self.assertRaises(RenderCancelled):
            Renderer(FFMPEG, cancel=cancel).export(self.project, output, overwrite=True)
        self.assertEqual(output.read_bytes(), b"existing output")
        self.assertFalse(list((self.project.root / ".cache").iterdir()))

    def test_short_source_holds_last_frame_and_landscape_export(self):
        self.project = Project.create(self.directory / "Landscape", "Landscape", aspect="16:9")
        self.project.add(title="Hold", visual=self.project.import_media(self.video), frames=40)
        output = self.directory / "landscape.mp4"
        Renderer(FFMPEG).export(self.project, output, draft=True)
        raw = self.ffmpeg("-i", str(output), "-map", "0:v:0", "-f", "framecrc", "-")
        self.assertIn(b"#dimensions 0: 960x540", raw)
        self.assertEqual(sum(line[:1].isdigit() for line in raw.splitlines()), 40)
