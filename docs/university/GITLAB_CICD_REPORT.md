# GitLab CI/CD и публикация

GitHub остаётся главным публичным репозиторием. `.gitlab-ci.yml` описывает сборку копии проекта в GitLab. Workflow GitHub отдельно запускает тесты для `main` и базовых веток цепочки v0.1.

| Стадия | Задание и результат |
|---|---|
| `quality` | Ruff и проверка компиляции Python |
| `unit` | Unittest для модели, файлов, блокировок и границ. JUnit XML |
| `integration` | Настоящий FFmpeg, CLI и окно Tk под Xvfb. JUnit XML |
| `e2e` | EFIR-E2E-01 через пользовательские команды. JUnit XML |
| `coverage` | Весь набор, включая GUI и дочерний CLI. Порог 95% строк. HTML, JSON, текст и XML Cobertura |
| `package` | Сборка образа, сохранение метаданных и прогон E2E внутри этого же образа без сети |
| `release` | Только защищённый тег `vMAJOR.MINOR.PATCH` и включённый Registry. Прогон E2E на публикационном образе перед отправкой |

GitLab показывает JUnit в интерфейсе тестов и Cobertura в изменениях Merge Request. HTML, XML и JSON хранятся 14 дней. Для pipeline с MR правило пропускает дублирующий push pipeline. Для сборки образа нужен Linux Docker runner с поддержкой Docker-in-Docker, а для публикации также включённый Container Registry. Dind работает в сетевом сегменте CI job. GitLab выдаёт `CI_REGISTRY`, `CI_REGISTRY_IMAGE`, `CI_REGISTRY_USER` и `CI_REGISTRY_PASSWORD`. Пароль передаётся на stdin встроенной команды. [Docker-in-Docker](https://docs.gitlab.com/ci/docker/using_docker_build/) · [Отчёты покрытия](https://docs.gitlab.com/ci/testing/code_coverage/)

## Подключение университетского репозитория

1. В существующей учётной записи GitLab создайте или выберите пустой учебный проект. Для курса подойдёт отдельный private проект либо проект учебной группы. Скопируйте URL Git.
2. Добавьте второй remote. Замените `GROUP` и при необходимости адрес сервера. Из ветки QA отправьте её и базовую ветку. Отправка `main` переносит историю с GitHub и не меняет GitHub. Если учебный проект уже имеет собственные коммиты, сначала сравните историю и согласуйте обычный merge.

```powershell
git remote add gitlab https://gitlab.com/GROUP/efir-studio.git
git push gitlab main
git push gitlab codex/efir-studio-v0.1 codex/efir-v01-audit codex/efir-v01-qa-ci
```

3. До переноса стабильной версии запустите pipeline на `codex/efir-v01-qa-ci`. Проверьте **Build → Pipelines** и JUnit, HTML, XML. После согласованного GitHub merge переносите актуальный `main` обычным push. Для автоматического pull mirror учитывайте тариф GitLab. Последовательный push отдельных веток подходит и без pull mirroring. [Pull mirroring](https://docs.gitlab.com/user/project/repository/mirror/pull/)
4. Для release job включите Container Registry и защитите шаблон тегов `v*` в настройках проекта. Релизный тег ставится на согласованный коммит. На GitLab Self-Managed согласуйте с администратором доступный runner с Dind.

**Фактический статус.** YAML разобран локально. Команды тестов и сборки имеют отдельные локальные и GitHub проверки с указанием среды в [отчёте](TESTING_REPORT.md). В рабочем репозитории настроен только GitHub remote, адрес и авторизация университетского GitLab отсутствуют. GitLab pipeline, серверный CI Lint и публикация образа ожидают привязки проекта.
