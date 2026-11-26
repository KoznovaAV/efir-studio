# Docker и Docker Compose

Образ выполняет публичный CLI и CPU-экспорт. Настольное Tk-окно запускается нативно, вместе с системными диалогами и приложениями Windows. Контейнеру для сборки нужны медиа, папка проекта и каталог результатов.

## Сборка и состав

`Dockerfile` фиксирует Python 3.13.16 slim и статические FFmpeg/FFprobe 8.1.1 по OCI digest. Итоговая стадия копирует два бинарника и код EFIR. Тестовые пакеты, Xvfb, модели и файлы пользователя в образ не входят. `.dockerignore` ограничивает контекст. Рабочий процесс имеет UID/GID 1000 и каталог `/workspace`. В Compose корень доступен на чтение, а для временных файлов и данных есть отдельные точки записи. Сеть контейнера отключена.

Статическая сборка берётся из открытого [рецепта wader/static-ffmpeg](https://github.com/wader/static-ffmpeg). Код EFIR распространяется по MIT. FFmpeg и связанные компоненты сохраняют свои лицензии. При распространении релизного образа прикладывайте сведения о сборке и соответствующих исходниках, включая компоненты libx264. [Лицензии FFmpeg](https://ffmpeg.org/legal.html)

## Запуск

```powershell
New-Item -ItemType Directory -Path workspace -Force
docker compose config --quiet
docker compose build
docker compose run --rm -T efir --help
docker compose run --rm -T efir init /workspace/episode --title 'Эфир'
```

Положите `clip.mp4` в `workspace` и выполните:

```powershell
docker compose run --rm -T efir add /workspace/episode --title 'Вступление' --visual /workspace/clip.mp4 --duration 2 --caption 'Мы в эфире.'
docker compose run --rm -T efir export /workspace/episode /workspace/episode.mp4 --draft
docker compose run --rm -T efir list /workspace/episode
```

Файлы на компьютере — `workspace/episode/` и `workspace/episode.mp4`. Каждый вызов инициирует отдельный контейнер с `--rm`. Папка остаётся на диске. Параметры меняются через среду процесса или локальный файл `.env`.

| Переменная Compose | Значение по умолчанию | Назначение |
|---|---|---|
| `EFIR_WORKSPACE` | `./workspace` | Существующая папка на компьютере, подключаемая к `/workspace` |
| `EFIR_IMAGE` | `efir-studio:local` | Имя и тег локального образа |
| `EFIR_UID`, `EFIR_GID` | `1000:1000` | На Linux укажите `id -u` и `id -g`, если папка принадлежит другому пользователю |

`EFIR_FFMPEG` в образе ведёт в `/usr/local/bin/ffmpeg`. Для нативного запуска используется обычный FFmpeg на компьютере, как в [README](../../README.md).

## Smoke и факт выполнения

```powershell
Get-Content -Raw -Encoding UTF8 tests/test_cli_e2e.py |
  docker compose run --rm -T -e EFIR_E2E_TEMP=/workspace --entrypoint python efir - -v
```

При необходимости PowerShell передаёт UTF-8 через `$OutputEncoding = [Text.UTF8Encoding]::new($false)`. В Bash вместо pipe можно использовать `< tests/test_cli_e2e.py`. Задание GitHub Actions запускает Compose, а GitLab запускает тот же E2E внутри исследуемого образа без сети.

26 ноября 2026 года Docker Engine 29.8.0 и Docker Compose v5.5.1 на Windows собрали Linux amd64-образ и выполнили реальный E2E. В отдельной последовательности Compose создал проект, добавил клип, экспортировал MP4 и заново прочитал проект из другого контейнера. Проект и MP4 сохранились на диске. Рантайм-образ содержит пользователя `1000:1000`. Локальная тестовая папка — `.efir-local/docker-qa/`.
