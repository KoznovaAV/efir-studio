# Руководство EFIR Studio v0.2

Для настройки на Windows используйте [установщик и лаунчер](AI_SETUP_GUIDE.md).

Нужны Python 3.11+ с Tk и FFmpeg с libx264 и AAC. Для Windows из корня репозитория:

```powershell
$env:EFIR_FFMPEG = 'D:\Tools\ffmpeg\bin\ffmpeg.exe'
python -m efir desktop
```

1. Создайте проект. Вручную добавьте сцену или введите идею в чат и нажмите **Предложить план**. Для чата должен работать Ollama с выбранной локальной моделью.
2. Просмотрите операции, подтвердите или отклоните предложение. Для отката выберите прежнюю ревизию слева и нажмите **Восстановить**.
3. В свойствах сцен прикрепите клипы или изображения, задайте длительность и субтитры. Поле **Реплика** используется синтезом, поле **Субтитр** используется при монтаже. Прикрепите готовую озвучку или нажмите **Озвучить** при настроенном Piper.
4. В списке задач просматривайте статус, отменяйте работу или повторяйте задачу после исправления ошибки. **Слушать** запускает WAV выбранной сцены в системном проигрывателе. Кнопка **Фон** импортирует фоновый трек.
5. **Экспорт MP4** создаёт фоновую задачу и сохраняет файл в `exports`. Откройте результат из списка задач. **Экспорт SRT** сохраняет отдельные субтитры. **Переносимая копия** создаёт проект с медиа и историей для другого компьютера.

## Подключение локальных движков

Установите Ollama и подготовьте одну модель отдельно. Запускайте движок с `OLLAMA_NO_CLOUD=1` на loopback, порт 11434. Выберите установленную модель через поле в приложении или `EFIR_OLLAMA_MODEL`. Ollama используется только для предложений. [Режим локального Ollama](https://docs.ollama.com/faq).

Установите Piper в отдельном окружении и подготовьте файлы `.onnx` и `.onnx.json` с нужными правами на использование.

```powershell
$env:EFIR_PIPER = 'D:\Tools\piper-env\Scripts\piper.exe'
$env:EFIR_PIPER_VERSION = '1.8.0'
$env:EFIR_PIPER_MODEL = 'D:\Tools\voices\ru_RU-denis-medium.onnx'
$env:EFIR_PIPER_SPEED = '1.0'
python -m efir desktop
```

Для миграции старого проекта используйте [руководство по миграции](MIGRATION_GUIDE.md).

```powershell
$p = '.\Мой выпуск'
python -m efir init $p --sqlite --title 'Мой выпуск'
python -m efir plan $p 'Добавь две сцены по 20 секунд'
python -m efir apply $p ID_ПЛАНА
python -m efir list $p
python -m efir edit $p ID_СЦЕНЫ --visual '.\clip.mp4'
python -m efir voice $p ID_СЦЕНЫ
python -m efir work $p --piper
python -m efir queue-export $p --draft
python -m efir work $p
python -m efir jobs $p
python -m efir srt $p '.\captions.srt'
python -m efir portable $p '.\Мой выпуск - копия'
```

`work` выполняет одну задачу. Запустите его с `--piper`, если в очереди есть озвучка. `voice --regenerate` инициирует новый дубль. При использовании Docker CLI все данные проекта остаются в смонтированной папке. Ollama и Piper подключаются в нативной версии отдельно. [Docker Compose](../../university/DOCKER_REPORT.md).
