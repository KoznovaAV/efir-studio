# Проверки v0.2

Новый набор покрывает миграции, конкурентную запись, перенос, сбой рабочего процесса, отмену, кэш, устаревшую реплику и ответы Ollama. Tk-тесты управляют реальными виджетами. FFmpeg E2E декодирует кадры и аудио и проверяет SRT. Модельные адаптеры проверяются заглушками протоколов и процессов.

## Подтверждённые прогоны

27 ноября 2026 года, коммит `623e2e8`.

| Окружение | Тесты | Строки | Ветвления |
|---|---:|---:|---:|
| Windows 11, локально | 68 прошли, 1 пропущен | 98,42% (1808/1837) | 96,40% (563/584) |
| Linux, локальный Docker | 69 прошли | 98,42% (1808/1837) | 96,75% (565/584) |
| GitHub Actions, Windows | 69 прошли | 98,48% (1809/1837) | 96,58% (564/584) |
| GitHub Actions, Ubuntu | 69 прошли | 98,31% (1806/1837) | 96,75% (565/584) |

На локальном Windows пропущен тест создания симлинка из-за ограничений прав. На GitHub тест прошёл. Windows-проверка первого коммита обнаружила, что тест миграции сравнивал канонический путь с исходной короткой формой Windows 8.3. После нормализации ожидаемого пути повторный прогон прошёл. [Прогон GitHub](https://github.com/KoznovaAV/efir-studio/actions/runs/45965439373).

В локальном Docker прошли quality, unit, integration, e2e, coverage и package. Package собрал Linux-образ и выполнил три CLI E2E внутри контейнера без сети. Контейнерная проверка GitHub также прошла. [Windows QA](https://github.com/KoznovaAV/efir-studio/actions/runs/45965439373/artifacts/15181914584), [Ubuntu QA](https://github.com/KoznovaAV/efir-studio/actions/runs/45965439373/artifacts/15182830836).

## Как повторить

```powershell
$env:EFIR_TEST_DESKTOP = '1'
$env:PYTHONUTF8 = '1'
python -m pip install -r requirements-qa.txt
python -m coverage run -m xmlrunner discover -s tests -v -o reports/qa/junit
python scripts/coverage_report.py
```

Нужен FFmpeg в PATH или `EFIR_FFMPEG`. На Ubuntu команда Tk запускается через `xvfb-run -a`. Порог 95% исполняемых строк включает каждый production-модуль. JUnit, Cobertura XML, HTML, JSON и текстовый отчёт записываются в `reports/qa`. [Проверки PR №4](https://github.com/KoznovaAV/efir-studio/pull/4/checks).
