# Резервный GitLab Runner на Windows

[Pipeline №812](https://vcs.uni-dubna.ru/KAV.24/efir-studio/-/pipelines/812), [job `lint` №5573](https://vcs.uni-dubna.ru/KAV.24/efir-studio/-/jobs/5573), сообщает об отсутствии подходящих активных runner с тегами `cluster`, `kubernetes`. Для резервного профиля подготовлен отдельный тег `efir-local`. Регистрация допустима, если учебный GitLab разрешает Project runner для этого проекта.

## Совместимость и граница доступа

- Публичная страница Help указывает ревизию `999a4a9a0bc`. Файл `VERSION` на этой ревизии — `14.9.1-ee`. Для регистрации выбран Runner **14.9.2**, последний патч ветки 14.9. В этой версии используется проектный registration token из блока **Set up a specific runner manually**. [Исходный VERSION](https://gitlab.com/gitlab-org/gitlab/-/raw/999a4a9a0bc/VERSION) · [Совместимость протокола](https://docs.gitlab.com/runner/register/)
- Windows видит `docker-desktop` на WSL2. Docker Engine 29.8.0 запустился с Linux x86-64, 12 логическими CPU и примерно 7,63 GiB RAM, доступной Docker VM. На диске D: перед проверкой было около 57,8 GiB свободного места.
- Runner — переносимый исполняемый файл Windows, работающий интерактивно от обычной учётной записи. Shell executor выполняет команды CI с правами этой записи. Поэтому runner запирается на EFIR Studio, принимает только тег `efir-local` на защищённых refs и запускается для проверенного кода. Доступ на запись к защищённым refs должен быть только у доверенных участников. [Ограничения Shell executor](https://docs.gitlab.com/runner/executors/shell/)
- Для Python-этапов Docker Desktop один раз собирает кэшируемый образ с FFmpeg и средствами тестирования. Задания запускаются в нём с UID 1000, без Linux capabilities и сети. Внутрь передаётся архив отслеживаемых исходников и отдельная папка для отчётов. `package` использует хостовый Docker Desktop для сборки и запуска того же образа с E2E. Job-контейнеры работают без `--privileged`, Docker socket, GitLab-токенов или настроенного для них Docker API. E2E внутри итогового образа запускается без сети. Задания выполняются последовательно, лимит контейнера — 4 CPU и 3 GiB RAM.
- Профиль выбирается только в точном университетском проекте на его защищённой ветке по умолчанию для push/web pipeline с переменной `EFIR_CI_RUNNER=local`. Остальные запуски по-прежнему используют университетский профиль.

## Подготовка и регистрация

1. В [Settings → CI/CD → Runners](https://vcs.uni-dubna.ru/KAV.24/efir-studio/-/settings/ci_cd) открыть блок **Set up a specific runner manually**. Если он отсутствует, отключён или преподаватель запрещает регистрацию, прекратить этот шаг и воспользоваться обращением ниже. Убедиться, что `main` является защищённой веткой.
2. Из обычного PowerShell загрузить и проверить официальный Windows amd64 binary 14.9.2. Для выполнения job служба не требуется. `config.toml` содержит выданный runner authentication token, поэтому остаётся в пользовательской папке.

```powershell
$runnerHome = Join-Path $env:LOCALAPPDATA 'EFIRStudio\gitlab-runner'
$runner = Join-Path $runnerHome 'gitlab-runner.exe'
$config = Join-Path $runnerHome 'config.toml'
$builds = Join-Path $runnerHome 'builds'
New-Item -ItemType Directory -Path $runnerHome -Force | Out-Null
if (!(Test-Path -LiteralPath $runner)) {
    Invoke-WebRequest 'https://gitlab-runner-downloads.s3.amazonaws.com/v14.9.2/binaries/gitlab-runner-windows-amd64.exe' -OutFile $runner
}
$expected = '08D0C4A72751D741AA55237BAE074FBCDBB3E2487ED613E823D3DCE84D8824BA'
if ((Get-FileHash -LiteralPath $runner -Algorithm SHA256).Hash -ne $expected) {
    throw 'SHA-256 не совпадает с официальным выпуском.'
}
& $runner --version
```

[Контрольные суммы выпуска](https://gitlab-runner-downloads.s3.amazonaws.com/v14.9.2/release.sha256)

3. В том же окне ввести проектный registration token. Скрытый ввод не помещает токен в историю PowerShell. Переменная окружения удаляется из текущего процесса после регистрации.

```powershell
if (Test-Path -LiteralPath $config) { throw 'Сначала проверьте существующий config.toml.' }
$registration = Read-Host 'Проектный registration token GitLab' -AsSecureString
$env:REGISTRATION_TOKEN = [Net.NetworkCredential]::new('', $registration).Password
try {
    & $runner --log-level warn register --config $config --non-interactive `
        --url 'https://vcs.uni-dubna.ru/' --name 'EFIR Studio local' `
        --executor shell --shell powershell --builds-dir $builds `
        --limit 1 --request-concurrency 1 `
        --tag-list efir-local --run-untagged=false --locked=true --access-level ref_protected
    if ($LASTEXITCODE) { throw 'Проверьте причину отказа регистрации в GitLab.' }
} finally {
    Remove-Item Env:REGISTRATION_TOKEN -ErrorAction SilentlyContinue
    Remove-Variable registration -ErrorAction SilentlyContinue
}
```

4. В локальном `config.toml` проверить глобальное `concurrent = 1`. В единственном блоке `[[runners]]` выставить `limit = 1` и `request_concurrency = 1`. В GitLab проверить **Locked**, **Protected**, тег `efir-local` и выключенный **Run untagged jobs**. Токены вводятся только локально и не прикладываются к отчётам.

## Запуск, остановка и удаление

```powershell
docker desktop start --timeout 45
docker info --format '{{.OSType}}'
& $runner run --config $config --working-directory $runnerHome
```

Оставить окно открытым. В GitLab нажать **Run pipeline**, выбрать `main`, добавить переменную `EFIR_CI_RUNNER=local` и запустить. Должны появиться `lint-local`, `unit-local`, `integration-local`, `e2e-local`, `coverage-local` и `container-local`. Сверить runner в журнале, порог покрытия, отчёты JUnit/Cobertura/HTML и E2E внутри образа.

После окончания заданий остановить foreground-процесс через **Ctrl+C**. Для снятия привязки и удаления файлов выполнить следующие команды только после остановки и проверки, что папка относится к выделенному runner.

```powershell
& $runner unregister --config $config --name 'EFIR Studio local'
if ($LASTEXITCODE) { throw 'Снимите регистрацию в Settings → CI/CD → Runners перед удалением.' }
Remove-Item -LiteralPath $runner,$config
```

Команда сохраняет содержимое `builds` для отдельного просмотра и решения об очистке.

## Если требуется участие администратора

> Просьба назначить доступный runner для проекта `KAV.24/efir-studio` или подтвердить право зарегистрировать выделенный Project runner с тегом `efir-local` только для защищённой ветки `main`. В [job 5573](https://vcs.uni-dubna.ru/KAV.24/efir-studio/-/jobs/5573) сервер не нашёл активного runner с тегами `cluster`, `kubernetes`. Требуются Python-тесты с артефактами покрытия, а также разрешённый способ собрать Linux-образ и выполнить E2E в этом образе.
