# GitLab CI/CD и публикация

GitHub остаётся главным публичным репозиторием. Учебная версия опубликована в основном рефе `main` [университетского GitLab](https://vcs.uni-dubna.ru/KAV.24/efir-studio). Workflow GitHub проверяет публичную [ветку PR №3](https://github.com/KoznovaAV/efir-studio/pull/3).

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

## Репозиторий и история

Локальная QA-ветка использует `origin` для GitHub и `gitlab` как второй remote. В учебном `main` был один начальный коммит `770063e` со стандартным README GitLab. Коммит интеграции `7950dc5` имеет двумя родителями этот коммит и проверенную QA-версию `194fa0a`. Тем самым история учебного репозитория сохранена, дерево публикации соответствует PR №3 и включает изменения PR №1 и №2.

Команды преподавателя для проверки учебной версии:

```powershell
git clone https://vcs.uni-dubna.ru/KAV.24/efir-studio.git
Set-Location efir-studio
git log -1 --oneline
```

Синхронизация последующих изменений ведётся из QA-ветки. Перед отправкой нужно получить текущий `gitlab/main`, сохранить его родителем коммита интеграции, сравнить дерево с SHA QA-ветки и выполнить обычный fast-forward push. Для проверки можно сравнить `git diff gitlab/main origin/codex/efir-v01-qa-ci`.

## Проверка преподавателем

1. Войти в университетский GitLab и открыть [проект](https://vcs.uni-dubna.ru/KAV.24/efir-studio). Нужен доступ к проекту от учебной группы.
2. Перейти в [Build → Pipelines](https://vcs.uni-dubna.ru/KAV.24/efir-studio/-/pipelines), открыть запуск для `main` и сверить его SHA с проверяемым коммитом.
3. Для результатов JUnit открыть вкладку **Tests** конкретного pipeline. Для показателей покрытия открыть job `coverage` и найти строки `Line coverage` и `Branch coverage`. В артефактах job выбрать `reports/qa/html/index.html` и `reports/qa/coverage.xml`. Cobertura отображает покрытие изменений в Merge Request при наличии сравниваемого диффа.
4. Открыть лог `e2e` для результата CLI-теста. В `container` проверить запись сборки и результат того же теста внутри образа. Для самостоятельного выполнения используйте [команды защиты](DEFENSE_GUIDE.md).

**Статус проверки.** Публикация кода в учебный `main` подтверждена через Git. Прежний доступ Git вернул HTTP 403 для версионного и проектных API. Серверный CI Lint, статус runner, результат pipeline и доступность Registry требуют проверки с доступом API. Результаты локальных проверок и GitHub описаны в [отчёте о тестировании](TESTING_REPORT.md).
