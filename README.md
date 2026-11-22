# Windows Fieldkit

Пять независимых утилит для Windows 10/11 x64. Проверяют запись OBS, переносят коллекции сцен, сравнивают сетевые замеры, обрывы USB и расход батареи. У каждой утилиты свой EXE и справка `--help`.

Скачайте ZIP в [GitHub Releases](https://github.com/markushkaaa/windows-fieldkit/releases), распакуйте и запускайте EXE из PowerShell. Для проверки OBS нужен [FFmpeg](https://ffmpeg.org/download.html) в PATH или путь к нему через `--ffmpeg`. Первый релиз предварительный.

## Проверка OBS

В OBS включите WebSocket Server через **Tools → WebSocket Server Settings**. Откройте нужную сцену и введите пароль OBS при запросе. Команда откажется начинать тест, если уже идёт стрим или запись. Записанный файл остаётся в папке записи OBS.

```powershell
.\obs-preflight.exe --seconds 8 --track 1 --track 2 --output preflight.json
.\obs-preflight.exe --file .\test.mkv --track 1
```

Отчёт содержит интервалы чёрного видео, наличие дорожек, начало участков тишины и пиковый уровень. Пик от −0,1 dBFS отмечается как близкий к пределу. Это индикатор, а не доказательство клиппинга. Утилита проверяет скомпонованное видео и выбранные дорожки. Один перекрытый источник может не обнаружиться.

## Перенос OBS

На старом ПК экспортируйте коллекцию через **Scene Collection → Export**. На новом ПК распакуйте пакет в новую постоянную папку и импортируйте `collection.json` через **Scene Collection → Import**. Сохраните папку `assets`. Импортируйте только доверенные архивы.

```powershell
.\obs-transfer.exe pack .\scene-export.json .\studio.zip
.\obs-transfer.exe unpack .\studio.zip .\studio-restored --check-obs
```

`--check-obs` сверяет типы источников с запущенным локальным OBS. Уберите флаг для офлайн-импорта. В `checklist.json` будут источники и устройства, требующие перепривязки, фильтры и отсутствующие типы источников. Переносятся сцены, компоновка и поддерживаемые локальные картинки, видео и текстовые файлы. URL браузерных источников, настройки стрима, фильтры и настройки неизвестных плагинов пропускаются. Встроенный текст включается только через `--include-text`. Проверьте названия сцен и содержимое медиа перед передачей пакета.

## USB

Выберите один большой тестовый файл на внешнем SSD. Утилита читает файл и опрашивает наличие того же Windows-тома. Используйте одинаковый файл и параметры для двух прогонов.

```powershell
.\usb-check.exe run E:\sample.bin --seconds 60 --output before.json
.\usb-check.exe run E:\sample.bin --seconds 60 --output after.json
.\usb-check.exe compare before.json after.json --output usb-comparison.json
```

Отчёт фиксирует исчезновения тома, повторные появления, ошибки чтения и объём прочитанных данных. Короткий обрыв может пройти между опросами. Чтение использует системный кэш. Причину обрыва нужно устанавливать сравнением условий.

## Сеть

Запустите сборщик на первом ПК, подставив его IPv4-адрес в локальной сети. Для второго ПК может потребоваться разрешить входящий доступ на частном сетевом профиле Windows. Сессия использует локальный HTTP и одноразовый токен. Используйте доверенную сеть.

```powershell
.\network-check.exe serve --bind 192.168.1.20 --folder session-1
```

Запустите следующие команды почти одновременно, каждую на своём ПК. Подставьте IP сборщика, IP роутера и показанный сборщиком токен.

```powershell
.\network-check.exe join http://192.168.1.20:8765 --token TOKEN --gateway 192.168.1.1 --device pc1
.\network-check.exe join http://192.168.1.20:8765 --token TOKEN --gateway 192.168.1.1 --device pc2
.\network-check.exe compare session-1\pc1.json session-1\pc2.json
```

CSV содержит время, разницу между замерами и RTT обоих ПК. Если внешняя точка блокирует ICMP, выберите другую через `--external`. Совпадение сбоев помогает локализовать проблему, но само по себе не доказывает причину.

## Батарея

Отключите питание, зафиксируйте яркость и режим электропитания. Начните оба прогона с похожего процента заряда. Тест остановится при подключении питания или снижении заряда до 15%.

```powershell
.\battery-check.exe run --seconds 300 --workload cpu --output before.json
.\battery-check.exe run --seconds 300 --workload cpu --output after.json
.\battery-check.exe compare before.json after.json --output battery-comparison.json
```

`cpu` нагружает один поток повторяемой задачей SHA-256. `idle` только снимает показания. Сравнение показывает изменение процента, расход в мВт⋅ч и медианную мощность, когда Windows выдаёт эти данные. Короткий замер не прогнозирует автономность.

## Сборка

Используйте 64-битный Python 3.12 на Windows.

```powershell
python -m pip install -r requirements-build.txt
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m unittest discover -s tests -v
.\scripts\build.ps1
```

Архивы и контрольные суммы появятся в `dist`. Исходники запускаются как модули, например `python -m fieldkit.usb_check --help`. Статус проверок на оборудовании указан в примечаниях к релизу.
