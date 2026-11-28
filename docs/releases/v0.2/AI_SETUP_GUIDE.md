# Локальные движки на Windows

Нужны Windows AMD64, PowerShell 7, CPython 3.13 x64 и действующий драйвер NVIDIA для GPU. FFmpeg берётся из EFIR_FFMPEG, локальной копии проекта или PATH. Перед загрузкой убедитесь, что на диске проекта есть не менее 12 GiB свободного места. Модель и движки устанавливаются в .efir-local/ai.

```powershell
pwsh -File scripts/setup-ai.ps1
pwsh -File scripts/start-studio.ps1
```

Установщик использует архив Ollama 0.40.19, закреплённый digest Qwen3 4B Q4_K_M, Piper 1.8.0 и голос Denis на базе rdh. Крупные файлы проверяются по полному SHA-256. Зависимости Piper устанавливаются в отдельное окружение по хешам пакетов. При повторном запуске установщик использует проверенные локальные файлы. Загрузка возобновляется с уже сохранённых диапазонов.

```powershell
pwsh -File scripts/setup-ai.ps1 -Component Piper
pwsh -File scripts/start-ai.ps1
pwsh -File scripts/check-ai.ps1
pwsh -File scripts/stop-ai.ps1
```

start-studio задаёт пути в окружении процесса, проверяет локальный сервер и запускает Tk. Если он сам запустил Ollama, при выходе студии выгружает модель и завершает свой процесс. Сервер ограничен адресом 127.0.0.1:11434, а OLLAMA_NO_CLOUD=1 отключает облачные функции. Если порт уже занят, установщик оставляет текущий процесс владельцу. Ручной режим по-прежнему запускается командой python -m efir desktop.

Дополнительные проверки используют psutil из изолированного QA-окружения. В обычном CI движки и веса отсутствуют. Реальные проверки запускаются отдельно и выводят причину пропуска, если компоненты не подготовлены.

```powershell
pwsh -File scripts/benchmark-ai.ps1 -Kind tts -Output reports/tts-new
pwsh -File scripts/benchmark-ai.ps1 -Kind llm -Output reports/llm-new
```

[Лицензии и происхождение](THIRD_PARTY_LICENSES.md) · [Проверка безопасности](SECURITY_REVIEW.md)
