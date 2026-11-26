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

## Подтверждённый статус GitLab

[Pipeline №809](https://vcs.uni-dubna.ru/KAV.24/efir-studio/-/pipelines/809) создан для коммита `2e7e8f1` в `main`. На момент проверки 26 ноября 2026 года интерфейс показывает `pending` и метку `stuck`.

| Стадия | Наблюдаемый результат №809 |
|---|---|
| Quality | `lint` ожидает запуска |
| Unit | Job создан, выполнения ещё нет |
| Integration | Job создан, выполнения ещё нет |
| E2E | Job создан, выполнения ещё нет |
| Coverage | Job создан, выполнения ещё нет |
| Package | Job создан, выполнения ещё нет |

Вкладка **Tests** показывает 0 выполненных тестов. JUnit, Cobertura и HTML появятся после выполнения заданий. Серверный CI Lint, версия GitLab, тип доступного runner и состояние Registry требуют доступа к соответствующим страницам или API. Наличие шести заданий подтверждает, что GitLab создал pipeline по конфигурации, но ничего не говорит о выполнении этих заданий.

## Подбор университетского runner

Для [pipeline №811](https://vcs.uni-dubna.ru/KAV.24/efir-studio/-/pipelines/811) пользователь подтвердил `pending / stuck`. В настройках проекта включены Shared runners. Доступен Shared Runner №1 с тегами `cluster` и `kubernetes`; рядом показан предупреждающий значок. Specific runners отсутствуют. Для диагностики нужны текст предупреждения, состояние runner и значение **Run untagged jobs**. Теги сами по себе не показывают ни тип executor, ни возможность принять задание.

Все задания теперь наследуют теги `cluster` и `kubernetes` из `default.tags`. Это устраняет несовпадение, если runner принимает только тегированные задания. Доступность проверяется назначением `lint` в новом pipeline. Тип executor подтверждается строкой `Preparing the "…" executor` в логе. Если `lint` по-прежнему ожидает назначения, нужно проверить, подходит ли runner для защищённой ветки `main`, и передать университетскому администратору предупреждение runner и сообщение из карточки job. [Подбор runner по тегам](https://docs.gitlab.com/ci/runners/configure_runners/#use-tags-to-run-jobs-on-different-platforms)

## Проверка Kubernetes и сборки образа

- Для `quality`, `unit`, `integration`, `e2e` и `coverage` нужен Linux-контейнер с указанным Python-образом. Установка пакетов в `integration` и `coverage` требует возможности запустить `apt-get` в контейнере и доступа к Debian и PyPI. Xvfb обслуживает тесты Tk. Журналы покажут, разрешены ли образы, сеть и установка пакетов политиками университета.
- `package` отдельно проверяет связь с Docker daemon командой `docker info`, собирает образ и запускает в нём CLI E2E без сети. Для существующего сервиса Docker-in-Docker Kubernetes runner должен разрешать привилегированный контейнер сервиса. Строка о Kubernetes executor в `lint` этого разрешения не подтверждает. [Docker-in-Docker на Kubernetes](https://docs.gitlab.com/ci/docker/using_docker_build/#docker-in-docker-with-tls-enabled-in-kubernetes)
- Если университет запрещает Docker-in-Docker, совместимые варианты нужно согласовать с администратором. Rootless BuildKit может собрать OCI-образ без Docker daemon при поддержке user namespaces и политик кластера. Для приёмки потребуется также разрешённый запуск именно этого образа с E2E, например на отдельном выделенном runner с разрешённым runtime. Если кластер уже предоставляет утверждённый runner для сборки и запуска контейнеров, `package` можно назначить на его тег. [Rootless BuildKit](https://docs.gitlab.com/ci/docker/using_buildkit/)

## Резервный профиль EFIR Studio

[Pipeline №812](https://vcs.uni-dubna.ru/KAV.24/efir-studio/-/pipelines/812), [`lint` №5573](https://vcs.uni-dubna.ru/KAV.24/efir-studio/-/jobs/5573) подтвердил, что GitLab не нашёл активного или доступного runner под теги `cluster`, `kubernetes`. Исполнение в Kubernetes и возможности Docker-in-Docker в университете остаются неподтверждёнными.

Резервный профиль `EFIR_CI_RUNNER=local` реализован для защищённого `main` именно этого проекта. Он использует Project runner с тегом `efir-local` и ту же группу Python-проверок, JUnit и порог покрытия, что университетский профиль. В `package` Docker Desktop собирает образ и выполняет E2E внутри него. Регистрация требует разрешения университета и ввода проектного registration token в локальном PowerShell. [Команды и обращение администратору](WINDOWS_RUNNER_FALLBACK.md)

### Локальная репетиция профиля

26 ноября 2026 года скрипты запуска проверены через штатный Windows PowerShell и Docker Desktop 29.8.0. Каждый этап запущен последовательно. Код проверки использовал обычные Linux-контейнеры на WSL2.

| Этап | Локальный результат |
|---|---|
| Quality | Ruff и `compileall` прошли |
| Unit | 17 тестов прошли |
| Integration | 23 теста прошли, включая Tk под Xvfb |
| E2E | CLI-тест прошёл |
| Coverage | Полный набор **41/41**. Строки **98,92% (825/834)**, ветви **96,58% (226/234)**. Порог 95% пройден |
| Package | Образ собран, CLI E2E внутри него прошёл с отключённой сетью |

JUnit, HTML, Cobertura, JSON и сведения об образе созданы локально этой же конфигурацией. Загрузка артефактов и результаты выполнения заданий в университетском GitLab требуют зарегистрированного runner. Анонимный серверный CI Lint недоступен. Запуск №812 подтверждает только стадию назначения runner.

Проверки кода и контейнера на GitHub прошли отдельно. [GitHub Actions](https://github.com/KoznovaAV/efir-studio/actions/runs/45885051835) · [Методика и локальные результаты](TESTING_REPORT.md)
