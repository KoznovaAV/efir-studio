"""Real AI acceptance probes. Run from the project root with configured engines."""

import argparse
import csv
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
from threading import Event, Thread, Timer
from time import perf_counter, sleep
import wave

import psutil

from efir.assistant import Ollama, decision, proposals, propose
from efir.jobs import Queue, run_one
from efir.project import EfirError
from efir.store import StudioProject
from efir.tts import Piper


RELEASE = Path("docs/releases/v0.2")
SPEECH = (
    "На частоте 33,3 МГц в 03:33 говорит диспетчер Анна Воронцова.",
    "Я запер замок, когда увидел замок. Повторите фамилии Козлова и Щербакова.",
    "Канал четыре... После паузы подождите две секунды. Не выключайте приёмник.",
    "Первая отметка — 12,5 процента. Следующая передача выйдет двадцать восьмого ноября.",
    "Дежурный Лев Гордеев передал микрофон Елене Евгеньевне.",
    "Атлас оказался новым атласом. На его полях нарисованы белки, а в перечне состава указаны белки.",
    "Канал ВГТРК, запасной вход HDMI. В записях МЧС указан адрес без номера.",
    "Дежурный поднял телефон, выслушал длинное молчание, затем записал в журнал, что переговоры "
    "прошли успешно. Через минуту телефон позвонил снова. На этот раз говорила сама тишина.",
    "Уважаемые зрители, бегущая строка временно идёт за вами. Не ускоряйтесь. "
    "Техник уже ищет кнопку, которой здесь никогда не было.",
)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Samples:
    """Record system headroom and the process trees belonging to the probe."""

    def __init__(self, extra_pid=None):
        self.roots = [psutil.Process()]
        if extra_pid is not None:
            self.roots.append(psutil.Process(extra_pid))
        self.done = Event()
        self.rows = []
        self.cpu = {}
        self.thread = Thread(target=self.sample, daemon=True)

    def __enter__(self):
        self.started = perf_counter()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.thread.join()

    def sample(self):
        gpu = None
        next_gpu = 0
        while True:
            elapsed = perf_counter() - self.started
            if elapsed >= next_gpu:
                try:
                    call = subprocess.run(
                        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=2, check=True,
                    )
                    gpu = int(call.stdout.splitlines()[0])
                except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                    pass
                next_gpu = elapsed + 0.8
            processes = {}
            for root in self.roots:
                try:
                    processes.update({p.pid: p for p in [root, *root.children(recursive=True)]})
                except psutil.NoSuchProcess:
                    continue
            rss = 0
            for process in processes.values():
                try:
                    if process.name().lower() == "nvidia-smi.exe":
                        continue
                    rss += process.memory_info().rss
                    times = process.cpu_times()
                    key = (process.pid, process.create_time())
                    total = times.user + times.system
                    base, _ = self.cpu.get(key, (total if process in self.roots else 0, total))
                    self.cpu[key] = base, total
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            self.rows.append({"seconds": round(elapsed, 3), "rss_mib": round(rss / 2**20, 2),
                              "available_mib": round(psutil.virtual_memory().available / 2**20, 2),
                              "gpu_mib": gpu})
            if self.done.wait(0.20):
                return

    def summary(self):
        return {"peak_rss_mib": max(row["rss_mib"] for row in self.rows),
                "min_available_mib": min(row["available_mib"] for row in self.rows),
                "peak_gpu_mib": max((row["gpu_mib"] for row in self.rows if row["gpu_mib"] is not None), default=None),
                "sampled_cpu_seconds": round(sum(last - base for base, last in self.cpu.values()), 3)}


def wave_duration(path):
    with wave.open(str(path), "rb") as audio:
        if audio.getsampwidth() != 2 or audio.getnchannels() != 1 or audio.getframerate() != 22050:
            raise EfirError("Ожидался PCM моно 22050 Гц, 16 бит.")
        return audio.getnframes() / audio.getframerate()


def speech_probe(directory):
    engine = Piper.configured()
    audio = directory / "wav"
    audio.mkdir()
    rows = []
    samples = {}
    for index, text in enumerate(SPEECH, 1):
        case = f"T{index:02d}"
        path = audio / (case + ".wav")
        start = perf_counter()
        with Samples() as monitor:
            engine.synthesize(text, path, cancel=Event())
            elapsed = perf_counter() - start
        length = wave_duration(path)
        rows.append({"id": case, "seconds": round(elapsed, 3), "wav_seconds": round(length, 3),
                     "rtf": round(elapsed / length, 3), "bytes": path.stat().st_size, **monitor.summary()})
        samples[case] = monitor.rows
        print(case, rows[-1], flush=True)
    write_json(directory / "references.json", dict(zip((row["id"] for row in rows), SPEECH, strict=True)))
    write_json(directory / "tts.json", {"recipe": engine.recipe, "cases": rows, "samples": samples})
    fields = ["id", "transcript", "pronunciation", "stress", "naturalness", "comments"]
    for listener in (1, 2):
        with (directory / f"listener-{listener}.csv").open("w", newline="", encoding="utf-8-sig") as output:
            csv.writer(output, delimiter=";").writerows([fields, *([row["id"]] for row in rows)])
    voice_queue_probe(directory, engine)


def voice_queue_probe(directory, engine):
    studio = StudioProject.create(directory / "Очередь", "Проверка Piper")
    first = studio.add(title="Сцена 1", frames=400, visual=None, speech=SPEECH[0])
    second = studio.add(title="Сцена 2", frames=400, visual=None, speech=SPEECH[1])
    studio.save()

    def synthesize(project, scene, tts, *, regenerate=False):
        queue = Queue(project)
        job = queue.enqueue("voice", scene_id=scene.id, recipe=tts.recipe, regenerate=regenerate)
        started = perf_counter()
        run_one(project, tts=tts, timeout=90)
        result = next(item for item in queue.jobs() if item["id"] == job)
        if result["state"] != "succeeded":
            raise EfirError(result["error"])
        return StudioProject.open(project.root), round(perf_counter() - started, 3), result["id"]

    studio, _, _ = synthesize(studio, first, engine)
    studio, _, _ = synthesize(studio, second, engine)
    studio, cached, cache_id = synthesize(studio, second, engine)
    studio, reused, reused_id = synthesize(studio, second, engine)
    initial = [scene.voice for scene in studio.scenes]
    studio.update(second.id, speech=SPEECH[2])
    studio.save()
    studio, repeat, _ = synthesize(studio, second, engine, regenerate=True)
    updated = [scene.voice for scene in studio.scenes]
    assert cache_id == reused_id and initial[0] == updated[0] and initial[1] != updated[1]

    faster = Piper.configured(1.2)
    fast_wav = directory / "speed-1.2.wav"
    faster.synthesize(SPEECH[0], fast_wav, cancel=Event())
    fast_duration = wave_duration(fast_wav)
    assert fast_duration < wave_duration(directory / "wav" / "T01.wav")
    cancelled_path = directory / "cancelled.wav"
    event = Event()
    cancel = Timer(0.8, event.set)
    cancel.start()
    try:
        engine.synthesize(SPEECH[-1] * 80, cancelled_path, cancel=event)
    except EfirError as error:
        if "отменена" not in str(error):
            raise
    else:
        raise AssertionError("Running Piper did not observe cancellation")
    finally:
        cancel.cancel()
    write_json(directory / "tts-queue.json",
               {"cache_seconds": cached, "reused_seconds": reused, "regenerate_scene_2_seconds": repeat,
                "assets_before": initial, "assets_after": updated, "fast_seconds": fast_duration,
                "cancelled": True})


class RecordedOllama(Ollama):
    def __init__(self, model, *, context, keep_alive, think):
        super().__init__(model)
        self.context = context
        self.keep_alive = keep_alive
        self.think = think
        self.response = None

    def request(self, path, payload=None):
        if path == "/api/chat":
            payload["options"]["num_ctx"] = self.context
            payload["keep_alive"] = self.keep_alive
            if self.think:
                payload["think"] = True
                payload["options"]["num_predict"] = 3400
            self.response = super().request(path, payload)
            return self.response
        return super().request(path, payload)


def benchmark_plans(directory, context, stay, server_pid, think):
    model = RecordedOllama(os.environ.get("EFIR_OLLAMA_MODEL", "qwen3:4b"),
                           context=context, keep_alive=stay, think=think)
    version = model.request("/api/version")["version"]
    tags = model.request("/api/tags")["models"]
    cases = [(int(n), text.strip()) for n, text in re.findall(
        r"^\| (\d+) \| ([^|]+) \|", (RELEASE / "LLM_EVALUATION.md").read_text(encoding="utf-8"), re.M,
    )]
    assert [n for n, _ in cases] == list(range(1, 21))
    piper_recipe = Piper.configured().recipe
    for number, prompt in cases:
        project = StudioProject.create(directory / f"case-{number:02d}", "Проверка Qwen3")
        if number > 3:
            fixture = directory / "fixture.png"
            if not fixture.exists():
                ffmpeg = os.environ.get("EFIR_FFMPEG", "ffmpeg")
                subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=s=16x16",
                                "-frames:v", "1", "-threads", "1", str(fixture)], check=True)
            visual = project.import_media(fixture)
            for title in ("Вступление", "Радио", "Финал"):
                project.add(title=title, speech="Мы начинаем передачу.", caption="Эфир", frames=250, visual=visual)
            project.save()
        before = project.document()
        model.response = None
        record = {"case": number, "prompt": prompt, "before": before, "base_revision": project.revision,
                  "ollama": version, "model": next(tag for tag in tags if tag["name"] == model.model),
                  "context": context, "keep_alive": stay, "think": think}
        started = perf_counter()
        with Samples(server_pid) as monitor:
            try:
                proposal = propose(project, model, prompt)
                record["operations"] = json.loads(next(row["operations"] for row in proposals(project)
                                                       if row["id"] == proposal))
                submitted = StudioProject.open(project.root)
                record["unchanged_before_approval"] = (
                    submitted.document() == before and submitted.revision == project.revision and not Queue(project).jobs()
                )
                assert record["unchanged_before_approval"]
                revision, _ = decision(project, proposal, accept=True, recipe=piper_recipe)
                result = StudioProject.open(project.root)
                record.update(after=result.document(), revision=revision, jobs=Queue(result).jobs())
            except EfirError as error:
                unchanged = StudioProject.open(project.root)
                assert unchanged.document() == before and unchanged.revision == project.revision
                record.update(error=str(error), unchanged_before_approval=True)
        record.update(seconds=round(perf_counter() - started, 3), raw=model.response,
                      samples=monitor.rows, **monitor.summary())
        write_json(directory / f"plan-{number:02d}.json", record)
        print(f"{number:02d}: {'error' if 'error' in record else 'applied'} {record['seconds']} s", flush=True)
        if record["min_available_mib"] < 2048:
            raise EfirError("Доступная RAM упала ниже 2 GiB. Замер сохранён, освободите память перед повтором.")


def streaming_request(model, context):
    prompt = {"model": model, "stream": True, "keep_alive": 30, "think": False,
              "options": {"temperature": 0.25, "seed": 42, "num_ctx": context, "num_predict": 120},
              "format": {"type": "object", "properties": {"text": {"type": "string", "maxLength": 120}},
                         "required": ["text"], "additionalProperties": False},
              "messages": [{"role": "user", "content": "Напиши короткую русскую реплику про туман над озером."}]}
    connection = http.client.HTTPConnection("127.0.0.1", 11434, timeout=90)
    first = None
    started = perf_counter()
    try:
        connection.request("POST", "/api/chat", json.dumps(prompt).encode(), {"Content-Type": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise EfirError(f"Ollama, потоковый замер HTTP {response.status}")
        while line := response.readline(65536):
            row = json.loads(line)
            if first is None and row.get("message", {}).get("content"):
                first = perf_counter() - started
            if row.get("done"):
                return {"first_token_seconds": first, "wall_seconds": perf_counter() - started, "api": row}
        raise EfirError("Ollama не завершил потоковый ответ.")
    finally:
        connection.close()


def profile_ollama(directory, context, server_pid):
    engine = Ollama(os.environ.get("EFIR_OLLAMA_MODEL", "qwen3:4b"))
    runs = []
    for trial in range(1, 4):
        engine.request("/api/generate", {"model": engine.model, "keep_alive": 0})
        for mode in ("cold", "warm"):
            with Samples(server_pid) as monitor:
                result = streaming_request(engine.model, context)
            runs.append({"trial": trial, "mode": mode, "context": context, **result, **monitor.summary(),
                         "models": engine.request("/api/ps")["models"], "samples": monitor.rows})
            print(trial, mode, f"{result['first_token_seconds']:.3f} s TTFT", flush=True)
        release = perf_counter()
        engine.request("/api/generate", {"model": engine.model, "keep_alive": 0})
        while engine.request("/api/ps")["models"]:
            if perf_counter() - release > 10:
                raise EfirError("Модель не выгрузилась из списка Ollama за 10 секунд.")
            sleep(0.1)
        runs[-1]["unload_seconds"] = perf_counter() - release
        write_json(directory / "profile.json", runs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("tts", "llm", "profile"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--context", type=int, default=8192)
    parser.add_argument("--keep-alive", type=int, default=0)
    parser.add_argument("--server-pid", type=int)
    parser.add_argument("--think", action="store_true")
    args = parser.parse_args()
    directory = args.output.resolve()
    directory.mkdir(parents=True)
    if args.kind == "tts":
        speech_probe(directory)
    elif args.kind == "profile":
        profile_ollama(directory, args.context, args.server_pid)
    else:
        benchmark_plans(directory, args.context, args.keep_alive, args.server_pid, args.think)


if __name__ == "__main__":
    main()
