# ESP32-C3 farm status display

Апаратний індикатор статусу ферми Bambu на матриці 8x8 (MAX7219), керований від farmwatch по USB serial.

## Мета

Одним поглядом на фізичний дисплей бачити стан усієї ферми (3 до 6 принтерів) читабельним текстом і цифрами: скільки друкує, де помилка, прогрес по кожному. Плюс яскравий сплив на подію (друк завершено, помилка). Дисплей не читає Bambu client напряму: дані бере farmwatch (єдиний процес читає CDP), тож конфлікту з рештою farmwatch нема.

## Залізо

- ESP32-C3 SuperMini (маленька китайська плата), живлення від USB ПК.
- 4 модулі 8x8 на MAX7219 у ряд (готова збірка FC16), разом 32x8. Саме така ширина дає читабельний текст; на одному модулі 8x8 текст не влазить.
- Wifi на ESP вимкнено повністю (WIFI_OFF + btStop): знімає пікові струми з живлення і робить роботу максимально стабільною.

Розводка (ESP підключається до входу ПЕРШОГО модуля; модулі зʼєднані між собою в ланцюг OUT->IN на самій платі):

| Модуль (вхід) | ESP32-C3 | Призначення |
|---------------|----------|-------------|
| VCC | 5V (VBUS) | живлення; 4 модулі при помірній яскравості, тримати intensity низькою |
| GND | GND | спільна земля |
| DIN | GPIO4 | дані (software SPI) |
| CS  | GPIO5 | load/latch |
| CLK | GPIO6 | такт |

MAX7219 живиться 5V, логіка ESP 3.3V, для збірки FC16 працює напряму. Яскравість тримаємо низькою (`setIntensity 2`), бо 4 модулі на повній яскравості можуть перевищити ліміт USB 500 mA. Якщо блимає або гасне: живити модулі від окремих 5V.

## Архітектура

Три частини, звʼязані текстовим протоколом по serial.

```
farmwatch (Python, ПК)                         ESP32-C3 (прошивка)
+-----------------------------+                +--------------------------+
| PrinterMonitor (CDP, thread)|                | Serial reader            |
|   on_update_complete  ------+--> SerialDisplay|   parse frame            |
|                             |    push(list)   |   store states[]         |
|                             |    sender thread|                          |
|                             |    every 2s --> | USB --> render loop 30fps|
+-----------------------------+   COM порт      |   bar chart + анімації   |
                                                +--------------------------+
```

### ПК: `serial_output.py` (новий модуль farmwatch)

`SerialDisplay` клас:
- `push(printers)`: приймає `List[PrinterStatus]`, будує текстовий кадр, зберігає як останній відомий (thread-safe). Не блокує потік монітора надовго.
- Внутрішній потік-відправник: раз на 2 секунди пише останній кадр у COM порт. Це стабільний heartbeat, незалежний від повільного циклу монітора (10 до 60 с). При помилці порту переоткриває його з backoff.
- `start()` / `stop()`. pyserial імпортується лениво; якщо його нема або порт недоступний, farmwatch працює як раніше (усе в try/except).
- Логер `logging.getLogger(__name__)`, повідомлення українською в стилі проєкту.

Вбудовування: там де створюється монітор (`telegram_bot.py`, `web/server.py`), якщо `config["serial"]["enabled"]`, чіпляємось ланцюжком до наявного `on_update_complete`, не перезатираючи:

```python
prev = monitor.on_update_complete
disp = SerialDisplay(port, baud); disp.start()
def _chained():
    if prev: prev()
    disp.push(monitor.get_all_printers())
monitor.on_update_complete = _chained
```

Конфіг, нова секція у `DEFAULT_CONFIG` (appconfig.py) і `config.example.json`:

```json
"serial": { "enabled": false, "port": "auto", "baud": 115200 }
```

`port: "auto"` (дефолт): плата шукається сама по USB VID (`autodetect_port()`):
спершу Espressif `0x303A` (native USB C3), далі мости CP210x/CH340/FTDI. Порт
перерішується на кожне відкриття, тож перепідключення чи зміна COM переживається.
Можна вписати конкретний `COM4`, тоді автопошук не задіюється.

### Протокол serial (текст, по рядку)

```
FW|<count>|<entry>,<entry>,...\n
entry = <letter><progress>      progress = 0..100
```

Мапа станів PrinterStatus.status у літеру:

| status   | letter | вигляд на матриці |
|----------|--------|-------------------|
| printing | p | цифра прогресу, напр. `72%` |
| idle     | i | `IDLE` |
| finished | d | `DONE` |
| stopped  | e | `ERR` |
| paused   | a | `PAUSE` |
| offline  | o | `OFF` |

Приклад: `FW|4|p72,i0,e0,d100` = 4 принтери: друкує 72%, простій, помилка, готово.
Порожня ферма: `FW|0|`. Кадр сам є heartbeat.

### ESP32-C3: прошивка (PlatformIO, MD_Parola)

Рендер тексту через MD_Parola (стандарт для MAX7219): біжучі рядки й цифри на 32x8.
`HARDWARE_TYPE = FC16_HW` для готової збірки 4-в-1 (якщо текст битий чи дзеркальний,
пробувати GENERIC_HW / ICSTATION_HW / PAROLA_HW).

- Serial 115200. Читає рядки, ігнорує биті, парсить кадр у `cur[]` (літера + прогрес).
- Яскравість помірна (`setIntensity 2`), бо 4 модулі на спільному живленні з USB.
- Екрани чергуються (кожен один прохід прокрутки, далі наступний):
  - зведення: `PRINT 3  ERR 1  IDLE 2  DONE 0` (плюс `PAUSE`/`OFF` якщо є);
  - деталі: `1 72%  2 IDLE  3 ERR  4 DONE  5 45%` (номер принтера + стан/прогрес).
- Події (перебивають поточний екран одразу, потім ротація триває): перехід у finished спливає `DONE <n>` ефектом знизу вгору; перехід у stopped спливає `ERR <n>`.
- Втрата звʼязку: нема кадру понад 6 с -> біжить `NO LINK`, щоб було видно що ПК/farmwatch зупинився.
- Порожня ферма: `NO PRINTERS`.
- До 8 принтерів. Якщо більше, показуються перші 8 (лог на ПК).

Проєкт прошивки живе у `firmware/` всередині репо (PlatformIO env `lolin_c3_mini`, native USB CDC), не заважає CI farmwatch (CI пакує лише Python exe).

## Тестування (перед кожним комітом)

- Python: юніт-тести формату кадру (fake PrinterStatus, точний рядок), відправник пишеться у fake serial обʼєкт (dependency injection, без заліза). `feed_demo.py` шле демо-дані у реальний COM для перевірки ESP без farmwatch.
- Прошивка: `pio run` компіляція як smoke-test. Прошивка/upload якщо плата підключена.
- Регрес: перевірити що farmwatch імпортується і працює при `serial.enabled=false` і без pyserial.

## Версії і CI

Версію рахує наявний CI (MINOR = git rev-list count), `version.py` руками не чіпаємо. Прошивка та модуль не додають нового exe у CI. `pyserial` додається у `requirements.txt`, PyInstaller підхопить його по імпорту.

## План робіт

1. Гілка `esp_display`, цей spec, коміт.
2. `serial_output.py` + тести формату та відправника, верифікація, коміт.
3. `firmware/` PlatformIO проєкт, `pio run` верифікація, коміт.
4. `feed_demo.py`, верифікація, коміт.
5. Вбудовування у telegram_bot.py та web/server.py, секція конфігу, requirements, регрес-перевірка, коміт.
6. README секція, коміт.
7. Мердж у main і один push (один реліз від CI).
