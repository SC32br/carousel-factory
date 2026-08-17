# Банк запросов Pinterest (референсы)

Искать лучше на английском. Оригиналы не публикуем: пин -> редроу. Ротировать запросы, не долбить один.

## Как парсить
Нужны куки в `secrets/pinterest_cookies.txt` (расширение «Get cookies.txt LOCALLY»). Конвейер зовёт gallery-dl сам. Вручную:

```
gallery-dl --cookies secrets/pinterest_cookies.txt --range 1-8 -d pins \
  "https://www.pinterest.com/search/pins/?q=quiet+luxury+desk"
```

Chrome в Docker-образе уже есть; на хосте поставь Chrome или Chromium и пропиши `CHROME_BIN`.

## Примеры под темы из themes.json
- бьюти: `hair salon aesthetic`, `beauty macro aesthetic`, `chrome nails 2026`
- стол: `productivity desk aesthetic`, `quiet luxury desk`
- еда: `cafe flatlay aesthetic`, `coffee editorial photography`
- дом: `quiet luxury interior`, `editorial home details`

Под свою нишу допиши запросы в `project/themes.json`, поле `queries`.
