"""Exercise the production Tk chat and queue with local AI and owned media."""

import argparse
from array import array
import json
import math
import os
from pathlib import Path
import re
import subprocess
from time import perf_counter, sleep
import tkinter as tk

from efir.jobs import ACTIVE, Queue
from efir.project import FPS, EfirError
from efir.render import export_srt, timestamp
from efir.store import StudioProject, export_file
from efir.tts import Piper
from efir.workspace import Workspace
from scripts.benchmark_ai import Samples, wave_duration, write_json


IDEA = (
    "Придумай новый выпуск «ЭФИР 03:33»: аварийное вещание ночью, аналоговый хоррор с абсурдом "
    "и чёрным юмором. Шесть связанных сцен ровно по 16 секунд. Соблюдай порядок: "
    "1 — приветствие, 2 — чайник в эфире, 3 — бесконечный коридор, 4 — архив завтрашнего дня, "
    "5 — вышка говорит имена, 6 — эфир зацикливается. Для speech пиши новые слова, "
    "которые произносит диктор в эфире от первого лица, по 12–20 слов. "
    "У каждой сцены — название, реплика, русские субтитры (главная фраза до 68 символов) и заметка."
)


def wait(window, condition, failure, timeout=140):
    deadline = perf_counter() + timeout
    while perf_counter() < deadline:
        window.update()
        if condition():
            return
        sleep(0.05)
    raise EfirError(failure())


def export_job(app, directory, label, timings):
    space = app.project
    known = {job["id"] for job in Queue(space).jobs()}
    started = perf_counter()
    app.draft.set(False)
    app.render()
    wait(app.window, lambda: not app.busy and any(job["id"] not in known and job["state"] not in ACTIVE
                                                for job in Queue(space).jobs()), app.status.get, timeout=180)
    job = next(job for job in Queue(space).jobs() if job["id"] not in known)
    if job["state"] != "succeeded":
        raise EfirError(job["error"])
    result = json.loads(job["result"])
    output = export_file(space, result)
    timings[label] = {**result, "seconds": round(perf_counter() - started, 3)}
    export_srt(space, directory / (label + ".srt"))
    return output


def accept_chat(app, prompt, directory, label, check):
    revision = app.project.revision
    before = app.project.document()
    app.prompt.insert("1.0", prompt)
    start = perf_counter()
    app.ask_button.invoke()
    wait(app.window, lambda: not app.busy, app.status.get)
    plan = app.pending()
    if not plan:
        raise EfirError(app.status.get())
    assert app.project.document() == before and app.project.revision == revision
    operations = json.loads(plan["operations"])
    write_json(directory / (label + ".json"), {**plan, "operations": operations})
    (directory / (label + "-preview.txt")).write_text(app.conversation.get("1.0", "end"), encoding="utf-8")
    check(operations)
    app.apply_button.invoke()
    wait(app.window, lambda: app.project.revision != revision and not app.busy, app.status.get)
    return round(perf_counter() - start, 3)


def assert_six_scenes(operations):
    if (len(operations) != 6 or any(op["op"] != "add" or op["frames"] != 16 * FPS
                                     or not op.get("speech") or not op.get("caption")
                                     for op in operations)):
        raise EfirError("Сценарий должен дать шесть озвученных сцен по 16 секунд.")


def assert_second_edit(operations, scene_id):
    if (len(operations) != 1 or operations[0].get("op") != "edit" or operations[0].get("scene") != scene_id
            or operations[0].keys() != {"op", "scene", "speech"}):
        raise EfirError("План должен менять только реплику второй сцены.")


def voices(app, scene_ids):
    queue = Queue(app.project)
    recipe = Piper.configured().recipe
    jobs = [queue.enqueue("voice", scene_id=scene_id, recipe=recipe) for scene_id in scene_ids]
    wait(app.window, lambda: not any(job["id"] in jobs and job["state"] in ACTIVE for job in queue.jobs())
         and not app.busy, app.status.get, timeout=180)
    outcomes = [job for job in queue.jobs() if job["id"] in jobs]
    if any(job["state"] != "succeeded" for job in outcomes):
        raise EfirError(str(outcomes))
    for scene in app.project.scenes:
        if scene.id in scene_ids and wave_duration(app.project.asset(scene.voice)) > scene.frames / FPS:
            raise EfirError(f"Реплика сцены «{scene.title}» длиннее её тайминга. Сократите речь или продлите сцену.")


def ffmpeg(*args):
    return subprocess.run([os.environ.get("EFIR_FFMPEG", "ffmpeg"), "-v", "error", "-nostdin", *args],
                          capture_output=True, check=True).stdout


def pcm(path):
    values = array("h")
    values.frombytes(ffmpeg("-i", path, "-map", "0:a:0", "-af", "highpass=f=180",
                           "-ac", "1", "-ar", "8000", "-f", "s16le", "-"))
    return values


def verify_timeline(project, result):
    video = ffmpeg("-i", result, "-map", "0:v:0", "-f", "framecrc", "-").decode()
    packets = [int(line.split(",")[2]) for line in video.splitlines() if line.startswith("0,")]
    assert "#dimensions 0: 1080x1920" in video and "#tb 0: 1/25" in video
    assert packets == list(range(2400))
    subtitles = ffmpeg("-i", result, "-map", "0:s:0", "-f", "srt", "-").decode("utf-8")
    boundaries = re.findall(r"\d\d:\d\d:\d\d,\d\d\d --> \d\d:\d\d:\d\d,\d\d\d", subtitles)
    assert boundaries == [f"{timestamp(i * 400)} --> {timestamp((i + 1) * 400)}" for i in range(6)]
    audio = pcm(result)
    offsets = []
    for index, scene in enumerate(project.scenes):
        voice = pcm(project.asset(scene.voice))
        start = 2400
        reference = voice[start:start + 3200:4]
        origin = index * 16 * 8000 + start
        energy = sum(value * value for value in reference)

        def similarity(shift):
            mix = audio[origin + shift:origin + shift + 3200:4]
            total = energy * sum(sample * sample for sample in mix)
            return sum(x * y for x, y in zip(mix, reference)) / math.sqrt(total) if total else 0

        shift = max(range(-480, 481), key=similarity)
        score = similarity(shift)
        offsets.append({"scene": index + 1, "offset_ms": shift / 8, "correlation": round(score, 4)})
    assert all(abs(row["offset_ms"]) <= 40 and row["correlation"] >= 0.8 for row in offsets)
    return {"video_frames": len(packets), "fps": 25, "captions": boundaries,
            "audio_samples_at_8k": len(audio), "audio_alignment": offsets}


def pilot(directory, assets, server_pid):
    timings = {}
    started = perf_counter()
    window = tk.Tk()
    app = Workspace(window)
    try:
        project = StudioProject.create(directory / "ЭФИР 03.33 AI", "ЭФИР 03:33 · Сбой расписания")
        app.set_project(project)
        window.update()
        timings["desktop_start_seconds"] = round(perf_counter() - started, 3)
        with Samples(server_pid) as monitor:
            timings["chat_seconds"] = accept_chat(app, IDEA, directory, "story", assert_six_scenes)
            project = app.project
            for index, scene in enumerate(project.scenes):
                project.update(scene.id, visual=project.import_media(assets / f"clip-{index}.mp4"))
            project.background = project.import_media(assets / "room-tone.wav", voice=True)
            project.save("Авторские исходники и фон")
            app.reload()

            started = perf_counter()
            voices(app, [scene.id for scene in app.project.scenes])
            timings["six_voices_seconds"] = round(perf_counter() - started, 3)
            first = export_job(app, directory, "first", timings)
            write_json(directory / "first-pass.json", timings)
            old = app.project.scenes[:]
            request = ("Измени только реплику второй сцены на «Чайник объявил траур по закипевшей воде. "
                       "Просим зрителей соблюдать минуту кипения». Оставь длительность, субтитры и остальные сцены.")
            timings["edit_chat_seconds"] = accept_chat(
                app, request, directory, "edit", lambda operations: assert_second_edit(operations, old[1].id),
            )
            started = perf_counter()
            voices(app, [old[1].id])
            timings["one_voice_seconds"] = round(perf_counter() - started, 3)
            edited = app.project.scenes
            assert all(a == b for index, (a, b) in enumerate(zip(old, edited, strict=True)) if index != 1)
            assert old[1].voice != edited[1].voice and old[1].visual == edited[1].visual and old[1].frames == edited[1].frames
            final = export_job(app, directory, "final", timings)
            assert timings["final"]["encoded"] == 1 and timings["final"]["reused"] == 5
            project = app.project
            copy = project.portable(directory / "Перенос")
            app.open(copy)
            assert app.project.document() == project.document()
            # A separate revision requests a fresh render from the portable media.
            app.project.save("Проверка нового экспорта из копии")
            app.reload()
            export_job(app, directory, "portable", timings)
            timeline = verify_timeline(project, final)
        sizes = {name: sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
                 for name, path in (("project", project.root), ("cache", project.root / ".cache"), ("portable", copy))}
        write_json(directory / "pilot.json", {"timings": timings, "timeline": timeline, "bytes": sizes,
                   "first": first.relative_to(directory).as_posix(), "final": final.relative_to(directory).as_posix(),
                   "samples": monitor.rows, **monitor.summary()})
        print(final, flush=True)
    finally:
        window.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("assets", type=Path)
    parser.add_argument("--server-pid", type=int)
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True)
    pilot(directory, args.assets.resolve(), args.server_pid)


if __name__ == "__main__":
    main()
