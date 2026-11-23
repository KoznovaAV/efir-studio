from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import Barrier, Event
import unittest
from unittest.mock import patch

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

    def test_nonempty_damaged_segment_is_rebuilt(self):
        self.project.add(title="Движение", visual=self.clip, frames=21, caption="Проверка")
        output = self.directory / "repair.mp4"
        renderer = Renderer(FFMPEG)
        renderer.export(self.project, output, draft=True)
        segment, = (self.project.root / ".cache").rglob("*.mkv")
        segment.write_bytes(segment.read_bytes()[:2048])
        result = renderer.export(self.project, output, overwrite=True, draft=True)
        self.assertEqual((result.encoded, result.reused), (1, 0))
        raw = self.ffmpeg("-i", str(output), "-map", "0:v:0", "-f", "framecrc", "-")
        self.assertEqual(sum(line[:1].isdigit() for line in raw.splitlines()), 21)
        result = renderer.export(self.project, output, overwrite=True, draft=True)
        self.assertEqual((result.encoded, result.reused), (0, 1))

    def test_source_audio_keeps_its_delay_relative_to_video(self):
        delayed = self.directory / "delayed.mkv"
        self.ffmpeg("-i", str(self.video), "-itsoffset", "0.40", "-i", str(self.audio),
                    "-map", "0:v", "-map", "1:a", "-c", "copy", str(delayed))
        self.project.add(title="Delayed audio", visual=self.project.import_media(delayed),
                         frames=20, start_frame=2, source_audio=True)
        output = self.directory / "delayed.mp4"
        Renderer(FFMPEG).export(self.project, output, draft=True)
        audio = self.ffmpeg("-i", str(output), "-map", "0:a:0", "-f", "s16le", "-")
        samples = memoryview(audio).cast("h")
        self.assertLess(max(abs(sample) for sample in samples[:int(0.30 * 48000 * 2)]), 50)
        self.assertGreater(max(samples[int(0.36 * 48000 * 2):int(0.60 * 48000 * 2)]), 1000)

    def test_parallel_mux_uses_its_own_manifest_and_captions(self):
        self.project.add(title="Первый", visual=self.clip, frames=9, caption="Альфа")
        second = self.project.add(title="Второй", visual=self.still, frames=6, caption="Бета")
        self.project.save()
        other = Project.open(self.project.root)
        other.move(second.id, 1)
        other.update(second.id, caption="Гамма")
        output_a, output_b = self.directory / "a.mp4", self.directory / "b.mp4"
        barrier = Barrier(2, timeout=15)

        class SynchronizedRenderer(Renderer):
            def _run(self, args, stage, **kwargs):
                if stage == "Сборка MP4":
                    barrier.wait()
                return super()._run(args, stage, **kwargs)

        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(SynchronizedRenderer(FFMPEG).export, self.project, output_a, draft=True)
            b = pool.submit(SynchronizedRenderer(FFMPEG).export, other, output_b, draft=True)
            a.result(timeout=30)
            b.result(timeout=30)
        opening_frames = []
        for output, opening, end_ms in ((output_a, "Альфа", "360"), (output_b, "Гамма", "240")):
            captions = self.ffmpeg("-i", str(output), "-map", "0:s:0", "-f", "srt", "-").decode()
            self.assertIn(f"00:00:00,000 --> 00:00:00,{end_ms}\n{opening}", captions)
            raw = self.ffmpeg("-i", str(output), "-map", "0:v:0", "-f", "framecrc", "-")
            frames = [line for line in raw.splitlines() if line[:1].isdigit()]
            self.assertEqual(len(frames), 15)
            opening_frames.append(frames[0].split(b",")[-1])
        self.assertNotEqual(*opening_frames)
        self.assertFalse(list((self.project.root / ".cache").rglob(".efir-*")))

    def test_export_uses_the_scene_snapshot_for_captions(self):
        first = self.project.add(title="Снимок", visual=self.clip, frames=10, caption="Исходная реплика")
        output = self.directory / "snapshot.mp4"

        def edit_during_export(message):
            if message == "Сборка MP4":
                self.project.update(first.id, frames=12, caption="Изменённая реплика")

        Renderer(FFMPEG, report=edit_during_export).export(self.project, output, draft=True)
        captions = self.ffmpeg("-i", str(output), "-map", "0:s:0", "-f", "srt", "-").decode()
        self.assertIn("00:00:00,000 --> 00:00:00,400\nИсходная реплика", captions)

    def test_cancel_after_ffmpeg_starts_preserves_existing_output(self):
        self.project.add(title="Экспорт", visual=self.clip, frames=250)
        output = self.directory / "existing.mp4"
        output.write_bytes(b"existing output")
        cancel = Event()
        processes = []
        popen = subprocess.Popen

        def cancel_after_start(command, **kwargs):
            process = popen(command, **kwargs)
            if "-filter_complex" in command:
                processes.append(process)
                cancel.set()
            return process

        with patch("efir.render.subprocess.Popen", side_effect=cancel_after_start):
            with self.assertRaises(RenderCancelled):
                Renderer(FFMPEG, cancel=cancel).export(self.project, output, overwrite=True, draft=True)
        process, = processes
        self.assertIsNotNone(process.poll())
        self.assertEqual(output.read_bytes(), b"existing output")
        self.assertFalse(list(self.directory.rglob(".efir-*")))

    def test_short_source_holds_last_frame_and_landscape_export(self):
        self.project = Project.create(self.directory / "Landscape", "Landscape", aspect="16:9")
        self.project.add(title="Hold", visual=self.project.import_media(self.video), frames=40)
        output = self.directory / "landscape.mp4"
        Renderer(FFMPEG).export(self.project, output, draft=True)
        raw = self.ffmpeg("-i", str(output), "-map", "0:v:0", "-f", "framecrc", "-")
        self.assertIn(b"#dimensions 0: 960x540", raw)
        self.assertEqual(sum(line[:1].isdigit() for line in raw.splitlines()), 40)
