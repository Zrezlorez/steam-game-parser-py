# Steam Catalog

[![CI](https://github.com/Zrezlorez/steam-game-parser-py/actions/workflows/ci.yml/badge.svg)](https://github.com/Zrezlorez/steam-game-parser-py/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE.md)

Steam Catalog собирает локальный каталог игр Steam в SQLite. Проект создан для упрощения
ИИ и ручного взаимодействия со списком игр: данные из Steam и SteamSpy сохраняются в
одном месте, после чего игры можно искать, фильтровать и экспортировать.

## Что умеет

- находит игры через SteamSpy или официальный список Steam;
- проверяет количество отзывов, доступность и статус выпуска;
- сохраняет описания, разработчиков, издателей, жанры, платформы и цены KZT/USD/UAH;
- сохраняет выборки полезных и свежих отзывов;
- импортирует личную библиотеку: владение, игровое время и дату последнего запуска;
- ищет игры и отзывы через SQLite FTS5;
- экспортирует подходящие игры в JSONL или CSV;
- безопасно прерывается через `Ctrl+C` и продолжает работу той же командой.

Все данные остаются на компьютере. SteamDB не используется.

## Запуск

Из корня проекта в PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
steam-catalog run
```

Если есть файл личной библиотеки:

```powershell
steam-catalog run --library .\data\steam_info.json
```

По умолчанию база создаётся в `data\steam_catalog.sqlite3`. Другой путь указывается
до команды:

```powershell
steam-catalog --database .\data\my_catalog.sqlite3 run
```

Повторный запуск продолжает сбор с того места, где остановился предыдущий:

```powershell
steam-catalog run
```

## Команды

| Команда | Назначение |
| --- | --- |
| `run` | Полный цикл: схема, импорт библиотеки, discovery и сбор |
| `init` | Создать или обновить схему SQLite |
| `import-library` | Импортировать личную библиотеку из JSON |
| `bootstrap` | Найти кандидатов и наполнить очередь |
| `collect` | Обработать одну игру или очередь |
| `collect-prices` | Догрузить цены USD и UAH |
| `refresh` | Повторно поставить устаревшие записи в очередь |
| `status` | Показать состояние каталога и очереди |
| `export` | Экспортировать игры в JSONL или CSV |
| `compact` | Очистить устаревшие строки HTTP-кэша и уменьшить базу |

Примеры:

```powershell
steam-catalog collect --limit 100
steam-catalog collect --appid 4000
steam-catalog collect-prices
steam-catalog status
steam-catalog export .\data\catalog.jsonl --format jsonl --include-reviews
steam-catalog export .\data\catalog.csv --format csv
```

## Настройки

| Переменная | По умолчанию | Назначение |
| --- | ---: | --- |
| `STEAM_CATALOG_DATABASE` | `data\steam_catalog.sqlite3` | Путь к SQLite |
| `STEAM_CATALOG_STEAM_WEB_API_KEY` | — | Ключ для `bootstrap --source steam` |
| `STEAM_CATALOG_MAX_ATTEMPTS` | `3` | Максимум попыток задания |
| `STEAM_CATALOG_STEAMSPY_ALL_INTERVAL_SECONDS` | `60` | Интервал запросов каталога SteamSpy |

Пример для текущего окна PowerShell:

```powershell
$env:STEAM_CATALOG_MAX_ATTEMPTS = "5"
steam-catalog run
```

## Источники

- SteamSpy — discovery и предварительное количество отзывов.
- Steam — официальный список приложений, Store, цены и отзывы.
- Для `bootstrap --source steam` нужен
  [Steam Web API key](https://steamcommunity.com/dev/apikey).

## Лицензия

[Apache License 2.0](LICENSE.md)
