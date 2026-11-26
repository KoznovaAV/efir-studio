"""CPU scene encoding, cache reuse and episode export via FFmpeg."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Event
import time

from .project import (AUDIO, FPS, IMAGE, VIDEO, FORMATS, EfirError, digest_file, publish, staged_file)

RENDER_VERSION = 2
SAMPLE_RATE = 48000
CACHE_SEGMENT = re.compile(r"[a-f0-9]{64}\.mkv")


def seconds(frames):
    return f"{frames // FPS}.{(frames % FPS) * (100 // FPS):02d}"


def timestamp(frames):
    total, fraction = divmod(frames, FPS)
    minutes, second = divmod(total, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d},{fraction * (1000 // FPS):03d}"


def subtitles(scenes):
    blocks = []
    start = 0
    for scene in scenes:
        end = start + scene.frames
        lines = [line.strip() for line in scene.caption.splitlines() if line.strip()]
        # SRT uses a blank line between cues and permits HTML-like styling.
        # Project captions are plain text.
        if lines:
            caption = "\n".join(lines).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            blocks.append(f"{len(blocks) + 1}\n{timestamp(start)} --> {timestamp(end)}\n{caption}\n")
        start = end
    return "\n".join(blocks)


def export_srt(project, destination, *, overwrite=False):
    destination = project.output(destination, ".srt")
    content = subtitles(project.scenes)
    if not content:
        raise EfirError("Добавьте субтитры хотя бы в одну сцену.")
    with staged_file(destination) as stage:
        stage.write_text(content, encoding="utf-8", newline="\n")
        publish(stage, destination, overwrite=overwrite)


class RenderCancelled(EfirError):
    pass


@dataclass(frozen=True)
class RenderResult:
    path: Path
    encoded: int
    reused: int


class Renderer:
    def __init__(self, ffmpeg=None, *, cancel=None, report=None):
        command = ffmpeg or os.environ.get("EFIR_FFMPEG") or "ffmpeg"
        self.ffmpeg = shutil.which(command)
        if not self.ffmpeg:
            raise EfirError("Нужен FFmpeg с libx264 и AAC. Укажите путь в EFIR_FFMPEG или добавьте FFmpeg в PATH.")
        self.cancel = cancel if cancel is not None else Event()
        self.report = report if report is not None else lambda message: None

    def _check_cancel(self):
        if self.cancel.is_set():
            raise RenderCancelled("Экспорт отменён.")

    def _run(self, args, stage, *, cwd=None, timeout=3600):
        self._check_cancel()
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            with subprocess.Popen(
                [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", *args],
                cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options
            ) as process:
                deadline = time.monotonic() + timeout
                try:
                    while True:
                        self._check_cancel()
                        if time.monotonic() >= deadline:
                            raise EfirError(f"{stage}: FFmpeg превысил допустимое время выполнения.")
                        try:
                            output, errors = process.communicate(timeout=0.2)
                            break
                        except subprocess.TimeoutExpired:
                            continue
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
        except OSError as error:
            raise EfirError(f"{stage}: не удалось запустить FFmpeg. {error}") from error
        if process.returncode:
            detail = errors.decode("utf-8", errors="replace").strip()[-2000:]
            raise EfirError(f"{stage}: FFmpeg завершился с кодом {process.returncode}.\n{detail}")
        return output

    def export(self, project, destination, *, overwrite=False, draft=False):
        destination = project.output(destination, ".mp4")
        if destination.exists() and not overwrite:
            raise EfirError(f"Файл уже существует: {destination}")
        scenes = tuple(project.scenes)
        if not scenes:
            raise EfirError("Добавьте хотя бы одну сцену.")
        width, height = FORMATS[project.aspect]
        if draft:
            width, height = width // 2, height // 2
        version = hashlib.sha256(self._run(["-version"], "Проверка FFmpeg", timeout=10)).hexdigest()
        cache = project.inside(".cache")
        cache.mkdir(exist_ok=True)
        assets = {}
        segments = []
        encoded = 0
        for index, scene in enumerate(scenes, 1):
            self._check_cancel()
            if scene.visual is None:
                raise EfirError(f"Добавьте видео или изображение для сцены «{scene.title}».")
            for ref in (scene.visual, scene.voice):
                if ref and ref not in assets:
                    assets[ref] = project.asset(ref)
            key_data = (RENDER_VERSION, version, width, height, scene.visual, scene.voice,
                        scene.frames, scene.start_frame, scene.fit, scene.source_audio)
            key = hashlib.sha256(json.dumps(key_data).encode()).hexdigest()
            directory = project.inside(".cache", key)
            segment = self._cached_segment(project, directory)
            label = f"{index}/{len(scenes)} · {scene.title}"
            if segment is not None:
                self.report(f"{label} · из кэша")
            else:
                self.report(f"{label} · кодирование")
                with staged_file(cache / "segment.mkv") as stage:
                    self._encode(scene, assets, stage, width, height, label)
                    segment = project.inside(".cache", key, digest_file(stage) + ".mkv")
                    self._check_cancel()
                    directory.mkdir(exist_ok=True)
                    self._publish_segment(stage, segment)
                encoded += 1
            segments.append((segment, scene.frames))

        self.report("Сборка MP4")
        manifest = "ffconcat version 1.0\n" + "".join(
            f"file {path.relative_to(cache).as_posix()}\nduration {seconds(frames)}\n"
            for path, frames in segments
        )
        with staged_file(cache / "scenes.ffconcat") as index, staged_file(cache / "captions.srt") as captions:
            index.write_text(manifest, encoding="ascii", newline="\n")
            text = subtitles(scenes)
            args = ["-y", "-protocol_whitelist", "file", "-f", "concat", "-safe", "1",
                    "-i", index.name]
            if text:
                captions.write_text(text, encoding="utf-8", newline="\n")
                args += ["-protocol_whitelist", "file", "-f", "srt", "-i", captions.name]
            background = getattr(project, "background", None)
            if background:
                args += ["-protocol_whitelist", "file", "-i", str(project.asset(background)),
                         "-filter_complex",
                         f"[{2 if text else 1}:a]volume=0.18[bed];"
                         "[0:a][bed]amix=inputs=2:duration=first:normalize=0,alimiter=latency=1[mix]"]
            args += ["-map", "0:v:0", "-map", "[mix]" if background else "0:a:0"]
            if text:
                args += ["-map", "1:0", "-c:s", "mov_text", "-metadata:s:s:0", "title=Captions"]
            args += ["-c:v", "copy",
                     "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4"]
            with staged_file(destination) as stage:
                self._run([*args, str(stage.resolve())], "Сборка MP4", cwd=cache)
                self._check_cancel()
                publish(stage, destination, overwrite=overwrite)
        return RenderResult(destination.resolve(), encoded, len(segments) - encoded)

    def _cached_segment(self, project, directory):
        for candidate in directory.glob("*.mkv"):
            self._check_cancel()
            if not CACHE_SEGMENT.fullmatch(candidate.name):
                continue
            segment = project.inside(".cache", directory.name, candidate.name)
            if segment.is_file() and digest_file(segment) == segment.stem:
                return segment
        return None

    @staticmethod
    def _publish_segment(stage, segment):
        try:
            # Another export may have published identical bytes during encoding.
            publish(stage, segment, overwrite=False)
        except EfirError:
            if digest_file(segment) != segment.stem:
                stage.replace(segment)

    def _encode(self, scene, assets, stage, width, height, label):
        visual = assets[scene.visual]
        args = ["-y", "-abort_on", "empty_output_stream", "-protocol_whitelist", "file"]
        if visual.suffix in IMAGE:
            args += ["-f", "image2", "-pattern_type", "none", "-loop", "1", "-framerate", str(FPS)]
            # Select the decoder by the accepted filename suffix.
            args += ["-c:v", "png" if visual.suffix == ".png" else "mjpeg"]
        else:
            args += ["-f", VIDEO[visual.suffix], "-ss", seconds(scene.start_frame)]
            if visual.suffix in (".mp4", ".mov"):
                args += ["-enable_drefs", "0", "-use_absolute_path", "0"]
        args += ["-i", str(visual)]
        if scene.voice:
            voice = assets[scene.voice]
            args += ["-protocol_whitelist", "file", "-f", AUDIO[voice.suffix]]
            if voice.suffix == ".m4a":
                args += ["-enable_drefs", "0", "-use_absolute_path", "0"]
            args += ["-i", str(voice)]

        # Use display aspect ratio before normalizing the output to square pixels.
        if scene.fit == "cover":
            geometry = (f"scale='max({width},2*ceil({height}*dar/2))':"
                        f"'max({height},2*ceil({width}/dar/2))',crop={width}:{height}")
        else:
            geometry = (f"scale='clip(2*trunc({height}*dar/2),2,{width})':"
                        f"'clip(2*trunc({width}/dar/2),2,{height})',"
                        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
        n_samples = scene.frames * (SAMPLE_RATE // FPS)
        filters = [
            f"[0:V:0]fps={FPS},{geometry},setsar=1,format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration={seconds(scene.frames)},"
            f"trim=end_frame={scene.frames},setpts=N/({FPS}*TB)[video]"
        ]
        audio = (f"aresample={SAMPLE_RATE}:async=1:first_pts=0,"
                 f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                 f"apad=whole_len={n_samples},atrim=end_sample={n_samples},asetpts=N/SR/TB")
        if scene.voice and scene.source_audio:
            filters += [f"[0:a:0]{audio},volume=0.25[clip]", f"[1:a:0]{audio},volume=0.75[voice]",
                        "[clip][voice]amix=inputs=2:normalize=0[audio]"]
        elif scene.voice or scene.source_audio:
            source = 1 if scene.voice else 0
            filters.append(f"[{source}:a:0]{audio}[audio]")
        else:
            filters.append(f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=end_sample={n_samples}[audio]")
        self._run(
            [*args, "-filter_complex_threads", "2", "-filter_complex", ";".join(filters),
             "-map", "[video]", "-map", "[audio]", "-c:v", "libx264", "-threads:v", "4",
             "-preset", "veryfast", "-crf", "19", "-profile:v", "high", "-bf", "0",
             "-c:a", "pcm_s16le", "-map_metadata", "-1", "-f", "matroska", str(stage)],
            label,
        )
