"""Optional Piper CPU adapter. No packages or voices are fetched by EFIR."""

import os
from pathlib import Path
import shutil
import subprocess
import time
import wave

from .project import EfirError, digest_file


class Piper:
    def __init__(self, executable, model, *, speed=1.0, version, timeout=120):
        self.executable = shutil.which(executable)
        self.model = Path(model).resolve()
        config = self.model.with_suffix(".onnx.json")
        if not self.executable or self.model.suffix != ".onnx" or not self.model.is_file() or not config.is_file():
            raise EfirError("Укажите Piper и локальные файлы голоса .onnx и .onnx.json.")
        if not 0.5 <= speed <= 2 or not version.strip():
            raise EfirError("Укажите версию Piper и скорость от 0,5 до 2.")
        self.timeout = timeout
        self.recipe = {"engine": "piper-cpu", "engine_version": version, "voice": self.model.stem,
                       "model_sha256": digest_file(self.model), "config_sha256": digest_file(config), "speed": speed}

    @classmethod
    def configured(cls, speed=None):
        try:
            speed = float(os.environ.get("EFIR_PIPER_SPEED", "1")) if speed is None else speed
        except ValueError as error:
            raise EfirError("EFIR_PIPER_SPEED должна быть числом от 0,5 до 2.") from error
        return cls(os.environ.get("EFIR_PIPER", "piper"), os.environ.get("EFIR_PIPER_MODEL", ""),
                   version=os.environ.get("EFIR_PIPER_VERSION", ""), speed=speed)

    def synthesize(self, text, destination, *, cancel):
        if cancel.is_set():
            raise EfirError("Озвучка отменена.")
        if (digest_file(self.model) != self.recipe["model_sha256"]
                or digest_file(self.model.with_suffix(".onnx.json")) != self.recipe["config_sha256"]):
            raise EfirError("Голос изменился после постановки задачи.")
        environment = {**os.environ, "PYTHONUTF8": "1"}
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        command = [self.executable, "-m", str(self.model), "-f", str(destination),
                   "--length-scale", str(1 / self.recipe["speed"]), "--sentence-silence", "0.15"]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=environment, **options) as process:
            deadline = time.monotonic() + self.timeout
            payload = (" ".join(text.splitlines()) + "\n").encode("utf-8")
            try:
                while True:
                    if cancel.is_set():
                        raise EfirError("Озвучка отменена.")
                    if time.monotonic() >= deadline:
                        raise EfirError("Piper превысил время синтеза.")
                    try:
                        process.communicate(input=payload, timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        payload = None
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        if process.returncode:
            raise EfirError(f"Piper завершился с кодом {process.returncode}. Проверьте версию и голос.")
        try:
            with wave.open(str(destination), "rb") as audio:
                if not audio.getnframes() or len(audio.readframes(audio.getnframes())) != (
                        audio.getnframes() * audio.getnchannels() * audio.getsampwidth()):
                    raise wave.Error("Invalid PCM data")
        except (wave.Error, EOFError, OSError) as error:
            raise EfirError("Piper не создал корректный WAV.") from error
