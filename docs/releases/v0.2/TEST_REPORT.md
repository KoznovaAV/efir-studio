# Проверки v0.2

Локальный нативный прогон регрессии до интеграции v0.2 выполнил 41 тест, 40 успешных, один пропущен из-за ограничений прав Windows на создание симлинков. Новый набор покрывает миграции, конкурентную запись, перенос, сбой рабочего процесса, отмену, кэш, устаревшую реплику и ответы Ollama. Tk-тесты управляют реальными виджетами. FFmpeg E2E декодирует кадры и аудио и проверяет SRT. Модельные адаптеры проверяются заглушками протоколов и процессов.

Первый Linux Docker-пакет прошёл три CLI E2E внутри нового образа с `--network none`. Первое измерение на Windows дало 96,21% строк и 90,77% ветвлений, при этом очистка файла БД в тесте миграции завершилась ошибкой из-за незакрытого тестового соединения. Измерение после исправлений фиксируется по итоговому запуску.

```powershell
$env:EFIR_TEST_DESKTOP = '1'
$env:PYTHONUTF8 = '1'
python -m pip install -r requirements-qa.txt
python -m coverage run -m xmlrunner discover -s tests -v -o reports/qa/junit
python scripts/coverage_report.py
```

Нужен FFmpeg в PATH или `EFIR_FFMPEG`. На Ubuntu команда Tk запускается через `xvfb-run -a`. Порог 95% исполняемых строк включает каждый production-модуль. JUnit, Cobertura XML, HTML, JSON и текстовый отчёт записываются в `reports/qa`. [Проверки на GitHub](https://github.com/KoznovaAV/efir-studio/actions).
