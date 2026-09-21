"""
lumen_lang.py — фиксированные строки бота (не ответы ИИ) на 25 языках топ-стран
Telegram (09.2026). Выбор per-chat (поле "lang"), переживает рестарты.
Первые 6 языков в STRINGS, новые — ПАКАМИ PACK_XX (дифф аддитивный).
Новый язык: код в SUPPORTED_LANGS + LANG_NAMES, перевод КАЖДОГО ключа
в STRINGS и PICK_TABLE (тест полноты не пустит пропуск). Обращение на "ты".
"""

from __future__ import annotations

# Порядок — по алфавиту кода языка: в меню кнопки идут в этом порядке.
SUPPORTED_LANGS: tuple[str, ...] = ("ar", "be", "bn", "de", "en", "es", "fa", "fil", "fr", "hi", "id", "it", "kk", "ms", "nl", "pl", "pt", "ru", "th", "tr", "uk", "ur", "uz", "vi", "zu")

# Нативные названия для кнопок меню (без флагов: флаг ≠ язык, см. план i18n).
LANG_NAMES: dict[str, str] = {
    "ar": "العربية",
    "be": "Беларуская",
    "bn": "বাংলা",
    "de": "Deutsch",
    "en": "English",
    "es": "Español",
    "fa": "فارسی",
    "fil": "Filipino",
    "fr": "Français",
    "hi": "हिन्दी",
    "id": "Bahasa Indonesia",
    "it": "Italiano",
    "kk": "Қазақша",
    "ms": "Bahasa Melayu",
    "nl": "Nederlands",
    "pl": "Polski",
    "pt": "Português",
    "ru": "Русский",
    "th": "ไทย",
    "tr": "Türkçe",
    "uk": "Українська",
    "ur": "اردو",
    "uz": "O'zbekcha",
    "vi": "Tiếng Việt",
    "zu": "isiZulu",
}

DEFAULT_LANG = "en"


def normalize_lang(code: str | None) -> str:
    """Приводит любой код к поддерживаемому; неизвестное/пустое → "en"."""
    c = (code or "").strip().lower()
    if c in LANG_NAMES:
        return c
    # "en-US"/"ru_RU" и подобное — берём базу до разделителя.
    base = c.replace("_", "-").split("-")[0]
    if base in LANG_NAMES:
        return base
    return DEFAULT_LANG


# key -> lang -> текст. Плейсхолдеры {error}/{choice}/{name} подставляются
# через t(..., **kwargs).
STRINGS: dict[str, dict[str, str]] = {
    # ── Меню /lang ──
    "lang_title": {
        "be": "Выберы мову бота:",
        "en": "Choose the bot language:",
        "es": "Elige el idioma del bot:",
        "kk": "Бот тілін таңда:",
        "ru": "Выбери язык бота:",
        "uk": "Вибери мову бота:",
    },
    "lang_done": {
        "be": "Мова: Беларуская.",
        "en": "Language: English.",
        "es": "Idioma: Español.",
        "kk": "Тіл: Қазақша.",
        "ru": "Язык: Русский.",
        "uk": "Мова: Українська.",
    },
    "lang_deny": {
        "be": "У групе мову мяняе толькі адміністратар, стваральнік групы або ўладальнік бота. У асабістых паведамленнях — даступна ўсім.",
        "en": "In groups only a group admin, the group creator or the bot owner can change the language. In direct messages it's available to everyone.",
        "es": "En grupos solo un administrador, el creador del grupo o el dueño del bot pueden cambiar el idioma. En mensajes directos está disponible para todos.",
        "kk": "Топта тілді тек әкімші, топ құрушы немесе бот иесі өзгерте алады. Жеке хабарламаларда бәріне қолжетімді.",
        "ru": "В группе язык меняет только администратор, создатель группы или владелец бота. В личных сообщениях — доступно всем.",
        "uk": "У групі мову змінює лише адміністратор, творець групи або власник бота. В особистих повідомленнях — доступно всім.",
    },
    # ── Описания команд (setMyCommands) ──
    "cmd_desc_start": {
        "be": "Пра бота і спіс каманд",
        "en": "About the bot and command list",
        "es": "Sobre el bot y lista de comandos",
        "kk": "Бот туралы және командалар тізімі",
        "ru": "О боте и список команд",
        "uk": "Про бота і список команд",
    },
    "cmd_desc_reset": {
        "be": "Ачысціць гісторыю дыялогу",
        "en": "Clear dialogue history",
        "es": "Borrar el historial del diálogo",
        "kk": "Диалог тарихын тазалау",
        "ru": "Очистить историю диалога",
        "uk": "Очистити історію діалогу",
    },
    "cmd_desc_draw": {
        "be": "Намаляваць малюнак па апісанні",
        "en": "Draw an image from a description",
        "es": "Dibujar una imagen a partir de una descripción",
        "kk": "Сипаттама бойынша сурет салу",
        "ru": "Нарисовать изображение по описанию",
        "uk": "Намалювати зображення за описом",
    },
    "cmd_desc_tts": {
        "be": "Агучыць тэкст",
        "en": "Voice text",
        "es": "Convertir texto en voz",
        "kk": "Мәтінді дауыстау",
        "ru": "Озвучить текст",
        "uk": "Озвучити текст",
    },
    "cmd_desc_lang": {
        "be": "Мова бота",
        "en": "Bot language",
        "es": "Idioma del bot",
        "kk": "Бот тілі",
        "ru": "Язык бота",
        "uk": "Мова бота",
    },
    # ── /start ──
    "start_text": {
        "be": (
            "<b>Lumen</b>\n\n"
            "Адказваю на пытанні (з пошукам у інтэрнэце, калі трэба), чытаю сайты і YouTube-відэа па спасылцы, разбіраю фота, відэа, аўдыя і дакументы, малюю малюнкі па апісанні і агучваю тэкст.\n\n"
            "<b>Каманды</b>\n"
            "/draw [апісанне] — намаляваць малюнак\n"
            "/tts [тэкст] — агучыць тэкст\n"
            "/reset — ачысціць гісторыю дыялогу\n"
            "/lang — мова бота\n\n"
            "Маляваць і агучваць можна і проста словамі, без каманд — напрыклад «намалюй ката» ці «агуч гэта».\n\n"
            "<b>TikTok</b>\n"
            "Дашлі спасылку — спампую відэа ці фота без вадзяных знакаў.\n\n"
            "Пытай што заўгодна — я слухаю."
        ),
        "en": (
            "<b>Lumen</b>\n\n"
            "I answer questions (with web search when needed), read websites and YouTube videos by link, analyze photos, videos, audio and documents, draw images from descriptions and voice text.\n\n"
            "<b>Commands</b>\n"
            "/draw [description] — draw an image\n"
            "/tts [text] — voice text\n"
            "/reset — clear dialogue history\n"
            "/lang — bot language\n\n"
            "You can also draw and voice just with words, no commands — e.g. “draw a cat” or “read this out loud”.\n\n"
            "<b>TikTok</b>\n"
            "Send a link — I'll download video or photos without watermarks.\n\n"
            "Ask anything — I'm listening."
        ),
        "es": (
            "<b>Lumen</b>\n\n"
            "Respondo preguntas (con búsqueda web cuando hace falta), leo sitios y videos de YouTube por enlace, analizo fotos, videos, audios y documentos, dibujo imágenes a partir de descripciones y convierto texto en voz.\n\n"
            "<b>Comandos</b>\n"
            "/draw [descripción] — dibujar una imagen\n"
            "/tts [texto] — convertir texto en voz\n"
            "/reset — borrar el historial del diálogo\n"
            "/lang — idioma del bot\n\n"
            "También puedes dibujar y dar voz con palabras, sin comandos — por ejemplo «dibuja un gato» o «lee esto en voz alta».\n\n"
            "<b>TikTok</b>\n"
            "Envía un enlace — descargaré el video o las fotos sin marcas de agua.\n\n"
            "Pregunta lo que quieras — te escucho."
        ),
        "kk": (
            "<b>Lumen</b>\n\n"
            "Сұрақтарға жауап беремін (қажет болса интернеттен іздеп), сілтеме бойынша сайттар мен YouTube-видеоларды оқимын, фото, видео, аудио және құжаттарды талдаймын, сипаттама бойынша сурет саламын және мәтінді дауыстаймын.\n\n"
            "<b>Командалар</b>\n"
            "/draw [сипаттама] — сурет салу\n"
            "/tts [мәтін] — мәтінді дауыстау\n"
            "/reset — диалог тарихын тазалау\n"
            "/lang — бот тілі\n\n"
            "Сурет салуды және дауыстауды командасыз, жай сөзбен де сұрауға болады — мысалы «мысық сал» немесе «мынаны дауыста».\n\n"
            "<b>TikTok</b>\n"
            "Сілтеме жібер — видеоны немесе фотоны су белгілерінсіз жүктеп беремін.\n\n"
            "Кез келген сұрақ қой — тыңдап тұрмын."
        ),
        "ru": (
            "<b>Lumen</b>\n\n"
            "Отвечаю на вопросы (с поиском в интернете, когда это нужно), читаю сайты и YouTube-видео по ссылке, разбираю фото, видео, аудио и документы, рисую изображения по описанию и озвучиваю текст.\n\n"
            "<b>Команды</b>\n"
            "/draw [описание] — нарисовать изображение\n"
            "/tts [текст] — озвучить текст\n"
            "/reset — очистить историю диалога\n"
            "/lang — язык бота\n\n"
            "Рисовать и озвучивать можно и просто словами, без команд — например «нарисуй кота» или «озвучь это».\n\n"
            "<b>TikTok</b>\n"
            "Пришли ссылку — скачаю видео или фото без водяных знаков.\n\n"
            "Спрашивай что угодно — я слушаю."
        ),
        "uk": (
            "<b>Lumen</b>\n\n"
            "Відповідаю на питання (з пошуком в інтернеті, коли треба), читаю сайти та YouTube-відео за посиланням, розбираю фото, відео, аудіо й документи, малюю зображення за описом та озвучую текст.\n\n"
            "<b>Команди</b>\n"
            "/draw [опис] — намалювати зображення\n"
            "/tts [текст] — озвучити текст\n"
            "/reset — очистити історію діалогу\n"
            "/lang — мова бота\n\n"
            "Малювати й озвучувати можна й просто словами, без команд — наприклад «намалюй кота» або «озвуч це».\n\n"
            "<b>TikTok</b>\n"
            "Надішли посилання — завантажу відео чи фото без водяних знаків.\n\n"
            "Питай що завгодно — я слухаю."
        ),
    },
    # ── Подсказки пустых команд ──
    "draw_empty": {
        "be": "Пазнач тэкст пасля каманды /draw. Прыклад: /draw касмічная станцыя",
        "en": "Add text after the /draw command. Example: /draw space station",
        "es": "Añade texto después del comando /draw. Ejemplo: /draw estación espacial",
        "kk": "/draw командасынан кейін мәтін жаз. Мысалы: /draw ғарыш станциясы",
        "ru": "Укажи текст после команды /draw. Пример: /draw космическая станция",
        "uk": "Вкажи текст після команди /draw. Приклад: /draw космічна станція",
    },
    "tts_empty": {
        "be": "Пазнач тэкст пасля каманды /tts. Прыклад: /tts Добры дзень",
        "en": "Add text after the /tts command. Example: /tts Good day",
        "es": "Añade texto después del comando /tts. Ejemplo: /tts Buenos días",
        "kk": "/tts командасынан кейін мәтін жаз. Мысалы: /tts Қайырлы күн",
        "ru": "Укажи текст после команды /tts. Пример: /tts Добрый день",
        "uk": "Вкажи текст після команди /tts. Приклад: /tts Добрий день",
    },
    "tts_too_long": {
        "be": "Тэкст занадта доўгі для агучкі (ліміт {limit} сімвалаў, зараз {length}). Скараці тэкст і паспрабуй зноў.",
        "en": "Text is too long for voicing (limit {limit} characters, now {length}). Shorten the text and try again.",
        "es": "El texto es demasiado largo para convertirlo en voz (límite de {limit} caracteres, ahora {length}). Acorta el texto e inténtalo de nuevo.",
        "kk": "Мәтін дауыстау үшін тым ұзын (лимит {limit} таңба, қазір {length}). Мәтінді қысқартып, қайталап көр.",
        "ru": "Текст слишком длинный для озвучки (лимит {limit} символов, сейчас {length}). Сократи текст и попробуй снова.",
        "uk": "Текст задовгий для озвучення (ліміт {limit} символів, зараз {length}). Скороти текст і спробуй знову.",
    },
    # ── Ошибки генерации изображений ──
    "draw_err_unavailable": {
        "be": "Сэрвіс генерацыі малюнкаў часова недаступны. Паспрабуй пазней.",
        "en": "The image generation service is temporarily unavailable. Try again later.",
        "es": "El servicio de generación de imágenes no está disponible ahora. Inténtalo más tarde.",
        "kk": "Сурет генерациялау қызметі уақытша қолжетімсіз. Кейінірек көріңіз.",
        "ru": "Сервис генерации изображений временно недоступен. Попробуй позже.",
        "uk": "Сервіс генерації зображень тимчасово недоступний. Спробуй пізніше.",
    },
    "draw_err_overloaded": {
        "be": "Сэрвіс генерацыі малюнкаў зараз перагружаны. Пачакай хвіліну і паспрабуй яшчэ раз.",
        "en": "The image generation service is overloaded right now. Wait a minute and try again.",
        "es": "El servicio de generación de imágenes está saturado ahora. Espera un minuto e inténtalo de nuevo.",
        "kk": "Сурет генерациялау қызметі қазір шамадан тыс жүктелген. Бір минут күтіп, қайталап көр.",
        "ru": "Сервис генерации изображений сейчас перегружен. Подожди минуту и попробуй ещё раз.",
        "uk": "Сервіс генерації зображень зараз перевантажений. Зачекай хвилину і спробуй ще раз.",
    },
    "draw_err_gone": {
        "be": "Сэрвіс генерацыі малюнкаў зараз недаступны. Паспрабуй пазней.",
        "en": "The image generation service is unavailable right now. Try again later.",
        "es": "El servicio de generación de imágenes no está disponible ahora. Inténtalo más tarde.",
        "kk": "Сурет генерациялау қызметі қазір қолжетімсіз. Кейінірек көріңіз.",
        "ru": "Сервис генерации изображений сейчас недоступен. Попробуй позже.",
        "uk": "Сервіс генерації зображень зараз недоступний. Спробуй пізніше.",
    },
    "draw_err_budget": {
        "be": "Генерацыя малюнка зараз займае занадта шмат часу. Паспрабуй, калі ласка, яшчэ раз праз хвіліну.",
        "en": "Image generation is taking too long right now. Please try again in a minute.",
        "es": "La generación de la imagen está tardando demasiado. Inténtalo de nuevo en un minuto, por favor.",
        "kk": "Сурет генерациялау қазір тым ұзаққа созылуда. Бір минуттан кейін қайталап көрші.",
        "ru": "Генерация изображения сейчас занимает слишком много времени. Попробуй, пожалуйста, ещё раз через минуту.",
        "uk": "Генерація зображення зараз займає надто багато часу. Спробуй, будь ласка, ще раз за хвилину.",
    },
    "draw_err_generic": {
        "be": "Памылка генерацыі малюнка. Паспрабуй яшчэ раз або перафармулюй апісанне.",
        "en": "Image generation failed. Try again or rephrase the description.",
        "es": "Error al generar la imagen. Inténtalo de nuevo o reformula la descripción.",
        "kk": "Сурет генерациялауда қате шықты. Қайталап көр немесе сипаттаманы басқаша жаз.",
        "ru": "Ошибка генерации изображения. Попробуй ещё раз или переформулируй описание.",
        "uk": "Помилка генерації зображення. Спробуй ще раз або переформулюй опис.",
    },
    # ── Ошибки озвучки ──
    "tts_err_exhausted": {
        "be": "Ліміт запытаў на агучку часова вычарпаны. Паспрабуй крыху пазней.",
        "en": "The text-to-speech request limit is temporarily exhausted. Try again a bit later.",
        "es": "El límite de solicitudes de voz está agotado temporalmente. Inténtalo un poco más tarde.",
        "kk": "Дауыстау сұраныстарының лимиті уақытша таусылды. Біраздан кейін көріңіз.",
        "ru": "Лимит запросов на озвучку временно исчерпан. Попробуй немного позже.",
        "uk": "Ліміт запитів на озвучення тимчасово вичерпано. Спробуй трохи пізніше.",
    },
    "tts_err_generic": {
        "be": "Не атрымалася агучыць тэкст. Паспрабуй яшчэ раз або скараці тэкст.",
        "en": "Couldn't voice the text. Try again or shorten the text.",
        "es": "No pude convertir el texto en voz. Inténtalo de nuevo o acorta el texto.",
        "kk": "Мәтінді дауыстау сәтсіз аяқталды. Қайталап көр немесе мәтінді қысқарт.",
        "ru": "Не получилось озвучить текст. Попробуй ещё раз или сократи текст.",
        "uk": "Не вдалося озвучити текст. Спробуй ще раз або скороти текст.",
    },
    # ── Лимиты и ошибки маршрута ──
    "rate_limited": {
        "be": "Ты адпраўляеш занадта шмат запытаў. Пачакай крыху.",
        "en": "You're sending too many requests. Wait a little.",
        "es": "Estás enviando demasiadas solicitudes. Espera un poco.",
        "kk": "Тым көп сұраныс жіберіп жатырсың. Біраз күте тұр.",
        "ru": "Ты отправляешь слишком много запросов. Подожди немного.",
        "uk": "Ти надсилаєш забагато запитів. Зачекай трохи.",
    },
    "model_err_rate_limit": {
        "be": "Ліміт запытаў зараз вычарпаны. Пачакай крыху і паспрабуй яшчэ раз.",
        "en": "The request limit is exhausted right now. Wait a bit and try again.",
        "es": "El límite de solicitudes está agotado ahora. Espera un poco e inténtalo de nuevo.",
        "kk": "Сұраныстар лимиті қазір таусылды. Біраз күтіп, қайталап көр.",
        "ru": "Лимит запросов сейчас исчерпан. Подожди немного и попробуй ещё раз.",
        "uk": "Ліміт запитів зараз вичерпано. Зачекай трохи і спробуй ще раз.",
    },
    "model_err_paid": {
        "be": "Сэрвіс часова недаступны. Паспрабуй паўтарыць запыт крыху пазней.",
        "en": "The service is temporarily unavailable. Try your request again a bit later.",
        "es": "El servicio no está disponible temporalmente. Intenta tu solicitud de nuevo un poco más tarde.",
        "kk": "Қызмет уақытша қолжетімсіз. Сұранысты біраздан кейін қайталап көр.",
        "ru": "Сервис временно недоступен. Попробуй повторить запрос чуть позже.",
        "uk": "Сервіс тимчасово недоступний. Спробуй повторити запит трохи пізніше.",
    },
    "model_err_forbidden": {
        "be": "Часовая памылка доступу да сэрвісу. Паспрабуй яшчэ раз.",
        "en": "A temporary service access error. Try again.",
        "es": "Error temporal de acceso al servicio. Inténtalo de nuevo.",
        "kk": "Қызметке қол жеткізуде уақытша қате. Қайталап көр.",
        "ru": "Временная ошибка доступа к сервису. Попробуй ещё раз.",
        "uk": "Тимчасова помилка доступу до сервісу. Спробуй ще раз.",
    },
    "model_err_unavailable": {
        "be": "Сэрвіс часова недаступны. Паспрабуй паўтарыць запыт крыху пазней.",
        "en": "The service is temporarily unavailable. Try your request again a bit later.",
        "es": "El servicio no está disponible temporalmente. Intenta tu solicitud de nuevo un poco más tarde.",
        "kk": "Қызмет уақытша қолжетімсіз. Сұранысты біраздан кейін қайталап көр.",
        "ru": "Сервис временно недоступен. Попробуй повторить запрос чуть позже.",
        "uk": "Сервіс тимчасово недоступний. Спробуй повторити запит трохи пізніше.",
    },
    "model_err_fallback": {
        "be": "Часовая памылка сэрвісу. Паспрабуй крыху пазней.",
        "en": "A temporary service error. Try again a bit later.",
        "es": "Error temporal del servicio. Inténtalo un poco más tarde.",
        "kk": "Қызметте уақытша қате. Біраздан кейін көріңіз.",
        "ru": "Временная ошибка сервиса. Попробуй чуть позже.",
        "uk": "Тимчасова помилка сервісу. Спробуй трохи пізніше.",
    },
    "err_quota_exhausted": {
        "be": "Бясплатны ліміт запытаў вычарпаны — гэта рэальны сутачны ліміт сэрвісу, а не памылка. Паспрабуй пазней.",
        "en": "The free request limit is exhausted — that's the service's real daily limit, not an error. Try again later.",
        "es": "El límite gratuito de solicitudes está agotado — es el límite diario real del servicio, no un error. Inténtalo más tarde.",
        "kk": "Тегін сұраныстар лимиті таусылды — бұл қате емес, қызметтің нақты күндік лимиті. Кейінірек көріңіз.",
        "ru": "Бесплатный лимит запросов исчерпан — это реальный суточный лимит сервиса, а не ошибка. Попробуй позже.",
        "uk": "Безкоштовний ліміт запитів вичерпано — це реальний добовий ліміт сервісу, а не помилка. Спробуй пізніше.",
    },
    "err_youtube_fail": {
        "be": "Не атрымалася адкрыць гэта відэа (магчыма, яно прыватнае, выдаленае, занадта доўгае або недаступнае для аналізу). Апішы, калі ласка, пра што яно словамі — тады змагу дапамагчы.",
        "en": "Couldn't open this video (it may be private, deleted, too long or unavailable for analysis). Please describe in words what it's about — then I can help.",
        "es": "No pude abrir este video (quizás sea privado, esté eliminado, sea demasiado largo o no esté disponible para analizar). Describe con palabras de qué trata — entonces podré ayudar.",
        "kk": "Бұл видеоны ашу сәтсіз аяқталды (жеке, жойылған, тым ұзын немесе талдауға қолжетімсіз болуы мүмкін). Не туралы екенін сөзбен сипаттап берші — сонда көмектесе аламын.",
        "ru": "Не получилось открыть это видео (возможно, оно приватное, удалено, слишком длинное или недоступно для анализа). Опиши, пожалуйста, о чём оно словами — тогда смогу помочь.",
        "uk": "Не вдалося відкрити це відео (можливо, воно приватне, видалене, занадто довге або недоступне для аналізу). Опиши, будь ласка, про що воно словами — тоді зможу допомогти.",
    },
    "err_budget": {
        "be": "Сэрвіс зараз перагружаны. Паспрабуй, калі ласка, яшчэ раз праз хвіліну.",
        "en": "The service is overloaded right now. Please try again in a minute.",
        "es": "El servicio está saturado ahora. Inténtalo de nuevo en un minuto, por favor.",
        "kk": "Қызмет қазір шамадан тыс жүктелген. Бір минуттан кейін қайталап көрші.",
        "ru": "Сервис сейчас перегружен. Попробуй, пожалуйста, ещё раз через минуту.",
        "uk": "Сервіс зараз перевантажений. Спробуй, будь ласка, ще раз за хвилину.",
    },
    # ── Ответ на провокации ──
    "injection_probe_reply": {
        "be": "Сваю настройку і інструкцыі я не раскрываю і не абмяркоўваю ў такім фармаце. Калі ў цябе звычайнае пытанне — задавай, з радасцю дапамагу.",
        "en": "I don't reveal or discuss my configuration and instructions in that format. If you have an ordinary question — ask away, I'll gladly help.",
        "es": "No revelo ni discuto mi configuración e instrucciones en ese formato. Si tienes una pregunta normal — hazla, con gusto ayudaré.",
        "kk": "Баптауым мен нұсқауларымды мұндай форматта ашпаймын және талқыламаймын. Қарапайым сұрағың болса — қой, қуана көмектесемін.",
        "ru": "Свою настройку и инструкции я не раскрываю и не обсуждаю в таком формате. Если у тебя обычный вопрос — задавай, с радостью помогу.",
        "uk": "Своє налаштування та інструкції я не розкриваю й не обговорюю в такому форматі. Якщо в тебе звичайне питання — питай, з радістю допоможу.",
    },
    # ── Занятость ──
    "lock_busy": {
        "be": "Папярэдні запыт яшчэ апрацоўваецца. Пачакай або паспрабуй пазней.",
        "en": "The previous request is still being processed. Wait or try again later.",
        "es": "La solicitud anterior aún se está procesando. Espera o inténtalo más tarde.",
        "kk": "Алдыңғы сұраныс әлі өңделуде. Күте тұр немесе кейінірек көріңіз.",
        "ru": "Предыдущий запрос ещё обрабатывается. Подожди или попробуй позже.",
        "uk": "Попередній запит ще обробляється. Зачекай або спробуй пізніше.",
    },
    "pick_lock_busy": {
        "be": "Пачакай, папярэдні запыт яшчэ апрацоўваецца.",
        "en": "Wait, the previous request is still being processed.",
        "es": "Espera, la solicitud anterior aún se está procesando.",
        "kk": "Күте тұр, алдыңғы сұраныс әлі өңделуде.",
        "ru": "Подожди, предыдущий запрос ещё обрабатывается.",
        "uk": "Зачекай, попередній запит ще обробляється.",
    },
    # ── Пометки стриминга ──
    "stream_note_send_fail": {
        "be": "\n\n[не атрымалася адправіць працяг паведамлення]",
        "en": "\n\n[couldn't send the rest of the message]",
        "es": "\n\n[no se pudo enviar el resto del mensaje]",
        "kk": "\n\n[хабарламаның жалғасын жіберу сәтсіз аяқталды]",
        "ru": "\n\n[не удалось отправить продолжение сообщения]",
        "uk": "\n\n[не вдалося надіслати продовження повідомлення]",
    },
    "stream_note_interrupted": {
        "be": "\n\n[злучэнне перарвалася — магчыма, адказ няпоўны]",
        "en": "\n\n[connection interrupted — the answer may be incomplete]",
        "es": "\n\n[conexión interrumpida — la respuesta puede estar incompleta]",
        "kk": "\n\n[байланыс үзілді — жауап толық болмауы мүмкін]",
        "ru": "\n\n[соединение прервалось — возможно, ответ неполный]",
        "uk": "\n\n[з'єднання перервалося — можливо, відповідь неповна]",
    },
    "inline_answer_title": {
        "be": "Адказ бота",
        "en": "Bot answer",
        "es": "Respuesta del bot",
        "kk": "Бот жауабы",
        "ru": "Ответ бота",
        "uk": "Відповідь бота",
    },
    # ── /reset ──
    "reset_deny": {
        "be": "У групе гісторыю скідае толькі адміністратар, стваральнік групы або ўладальнік бота. У асабістых паведамленнях — даступна ўсім.",
        "en": "In groups only a group admin, the group creator or the bot owner can reset the history. In direct messages it's available to everyone.",
        "es": "En grupos solo un administrador, el creador del grupo o el dueño del bot pueden borrar el historial. En mensajes directos está disponible para todos.",
        "kk": "Топта тарихты тек әкімші, топ құрушы немесе бот иесі тазалай алады. Жеке хабарламаларда бәріне қолжетімді.",
        "ru": "В группе историю сбрасывает только администратор, создатель группы или владелец бота. В личных сообщениях — доступно всем.",
        "uk": "У групі історію скидає лише адміністратор, творець групи або власник бота. В особистих повідомленнях — доступно всім.",
    },
    "reset_done": {
        "be": "Гісторыя дыялогу ў гэтым чаце ачышчана. Пачынаем з чыстага аркуша.",
        "en": "Dialogue history in this chat is cleared. Starting fresh.",
        "es": "Historial del diálogo en este chat borrado. Empezamos de cero.",
        "kk": "Бұл чаттағы диалог тарихы тазаланды. Таза парақтан бастаймыз.",
        "ru": "История диалога в этом чате очищена. Начинаем с чистого листа.",
        "uk": "Історію діалогу в цьому чаті очищено. Починаємо з чистого аркуша.",
    },
    # ── /logs и /stats (только владелец) ──
    "logs_deny": {
        "be": "Няма доступу да гэтай каманды.",
        "en": "No access to this command.",
        "es": "Sin acceso a este comando.",
        "kk": "Бұл командаға қолжетімділік жоқ.",
        "ru": "Нет доступа к этой команде.",
        "uk": "Немає доступу до цієї команди.",
    },
    "logs_group_only": {
        "be": "Гэтая каманда паказвае тэхнічныя логі — даступна толькі ў асабістых паведамленнях з ботам, не ў групах.",
        "en": "This command shows technical logs — available only in direct messages with the bot, not in groups.",
        "es": "Este comando muestra registros técnicos — disponible solo en mensajes directos con el bot, no en grupos.",
        "kk": "Бұл команда техникалық логтарды көрсетеді — тек ботпен жеке хабарламаларда қолжетімді, топтарда емес.",
        "ru": "Эта команда показывает технические логи — доступна только в личных сообщениях с ботом, не в группах.",
        "uk": "Ця команда показує технічні логи — доступна лише в особистих повідомленнях з ботом, не в групах.",
    },
    "logs_empty": {
        "be": "Лог-файл пусты або яшчэ не быў створаны.",
        "en": "The log file is empty or hasn't been created yet.",
        "es": "El archivo de registros está vacío o aún no se ha creado.",
        "kk": "Лог файлы бос немесе әлі жасалмаған.",
        "ru": "Лог-файл пуст или ещё не был создан.",
        "uk": "Лог-файл порожній або ще не був створений.",
    },
    "logs_send_error": {
        "be": "Памылка пры адпраўцы логаў: {error}",
        "en": "Error sending logs: {error}",
        "es": "Error al enviar los registros: {error}",
        "kk": "Логтарды жіберуде қате: {error}",
        "ru": "Ошибка при отправке логов: {error}",
        "uk": "Помилка під час надсилання логів: {error}",
    },
    "stats_deny": {
        "be": "Няма доступу да гэтай каманды.",
        "en": "No access to this command.",
        "es": "Sin acceso a este comando.",
        "kk": "Бұл командаға қолжетімділік жоқ.",
        "ru": "Нет доступа к этой команде.",
        "uk": "Немає доступу до цієї команди.",
    },
    "stats_group_only": {
        "be": "Гэтая каманда паказвае тэхнічную статыстыку — даступна толькі ў асабістых паведамленнях з ботам, не ў групах.",
        "en": "This command shows technical stats — available only in direct messages with the bot, not in groups.",
        "es": "Este comando muestra estadísticas técnicas — disponible solo en mensajes directos con el bot, no en grupos.",
        "kk": "Бұл команда техникалық статистиканы көрсетеді — тек ботпен жеке хабарламаларда қолжетімді, топтарда емес.",
        "ru": "Эта команда показывает техническую статистику — доступна только в личных сообщениях с ботом, не в группах.",
        "uk": "Ця команда показує технічну статистику — доступна лише в особистих повідомленнях з ботом, не в групах.",
    },
    "stats_no_data": {
        "be": "  няма даных",
        "en": "  no data",
        "es": "  sin datos",
        "kk": "  дерек жоқ",
        "ru": "  нет данных",
        "uk": "  немає даних",
    },
    "stats_limit_used": {
        "be": " (ліміт вычарпаны)",
        "en": " (limit exhausted)",
        "es": " (límite agotado)",
        "kk": " (лимит таусылды)",
        "ru": " (лимит исчерпан)",
        "uk": " (ліміт вичерпано)",
    },
    # ── TikTok ──
    "tiktok_sound_recognized": {
        "be": "Спасылка на гук TikTok распазнана.",
        "en": "TikTok sound link recognized.",
        "es": "Enlace de sonido de TikTok reconocido.",
        "kk": "TikTok дыбыс сілтемесі танылды.",
        "ru": "Ссылка на звук TikTok распознана.",
        "uk": "Посилання на звук TikTok розпізнано.",
    },
    "tiktok_sound_no_separate": {
        "be": "Спампаваць гук асобна па спасылцы на яго старонку не атрымаецца — ні TikWM, ні сам TikTok не аддаюць патрэбныя даныя па такім выглядзе спасылкі гэтаму боту. Дашлі, калі ласка, спасылку на любое відэа з гэтым гукам — бот дашле гук разам з ім.",
        "en": "Can't download the sound separately via its page link — neither TikWM nor TikTok itself give this bot the needed data for that link kind. Please send a link to any video with this sound — the bot will send the sound along with it.",
        "es": "No se puede descargar el sonido por separado con el enlace a su página — ni TikWM ni el propio TikTok dan a este bot los datos necesarios para ese tipo de enlace. Envía, por favor, un enlace a cualquier video con este sonido — el bot enviará el sonido junto con él.",
        "kk": "Дыбысты оның парақшасының сілтемесі арқылы жеке жүктеу мүмкін емес — мұндай сілтеме түріне TikWM де, TikTok-тың өзі де бұл ботқа қажетті деректерді бермейді. Осы дыбысы бар кез келген видеоға сілтеме жіберші — бот дыбысты онымен бірге жібереді.",
        "ru": "Скачать звук отдельно по ссылке на его страницу не получится — ни TikWM, ни сам TikTok не отдают нужные данные по такому виду ссылки этому боту. Пришлите, пожалуйста, ссылку на любое видео с этим звуком — бот пришлёт звук вместе с ним.",
        "uk": "Завантажити звук окремо за посиланням на його сторінку не вийде — ні TikWM, ні сам TikTok не віддають потрібні дані за таким видом посилання цьому боту. Надішли, будь ласка, посилання на будь-яке відео з цим звуком — бот надішле звук разом із ним.",
    },
    "tiktok_dl_hd": {
        "be": "Спампоўваю відэа без вадзяных знакаў (HD)",
        "en": "Downloading video without watermarks (HD)",
        "es": "Descargando video sin marcas de agua (HD)",
        "kk": "Су белгілерінсіз видео жүктелуде (HD)",
        "ru": "Скачиваю видео без водяных знаков (HD)",
        "uk": "Завантажую відео без водяних знаків (HD)",
    },
    "tiktok_dl_as_is": {
        "be": "Версіі без вадзяных знакаў не знайшлося — спампоўваю як ёсць",
        "en": "No watermark-free version found — downloading as is",
        "es": "No se encontró versión sin marcas de agua — descargando tal cual",
        "kk": "Су белгілерінсіз нұсқа табылмады — бар күйінде жүктелуде",
        "ru": "Версии без водяных знаков не нашлось — скачиваю как есть",
        "uk": "Версії без водяних знаків не знайшлося — завантажую як є",
    },
    "tiktok_dl_plain": {
        "be": "Спампоўваю відэа без вадзяных знакаў",
        "en": "Downloading video without watermarks",
        "es": "Descargando video sin marcas de agua",
        "kk": "Су белгілерінсіз видео жүктелуде",
        "ru": "Скачиваю видео без водяных знаков",
        "uk": "Завантажую відео без водяних знаків",
    },
    "tiktok_too_big": {
        "be": "Гэта відэа з TikTok занадта вялікае для адпраўкі — Telegram Bot API абмяжоўвае загрузку файлаў 50 МБ. Паспрабуй спампаваць гэта відэа іншым спосабам.",
        "en": "This TikTok video is too big to send — the Telegram Bot API limits file uploads to 50 MB. Try downloading this video another way.",
        "es": "Este video de TikTok es demasiado grande para enviarlo — la API de bots de Telegram limita la subida de archivos a 50 MB. Intenta descargar este video de otra forma.",
        "kk": "Бұл TikTok видеосы жіберу үшін тым үлкен — Telegram Bot API файл жүктеуді 50 МБ-мен шектейді. Видеоны басқа жолмен жүктеп көр.",
        "ru": "Это видео из TikTok слишком большое для отправки — Telegram Bot API ограничивает загрузку файлов 50 МБ. Попробуйте скачать это видео другим способом.",
        "uk": "Це відео з TikTok занадто велике для надсилання — Telegram Bot API обмежує завантаження файлів 50 МБ. Спробуй завантажити це відео іншим способом.",
    },
    "tiktok_no_media": {
        "be": "Спасылка распазнана, але TikTok не аддаў ні відэа, ні фота па ёй — магчыма, кантэнт выдалены або недаступны.",
        "en": "Link recognized, but TikTok gave neither video nor photos for it — the content may be deleted or unavailable.",
        "es": "Enlace reconocido, pero TikTok no devolvió ni video ni fotos — quizás el contenido fue eliminado o no está disponible.",
        "kk": "Сілтеме танылды, бірақ TikTok ол бойынша не видео, не фото бермеді — контент жойылған немесе қолжетімсіз болуы мүмкін.",
        "ru": "Ссылка распознана, но TikTok не отдал ни видео, ни фото по ней — возможно, контент удалён или недоступен.",
        "uk": "Посилання розпізнано, але TikTok не віддав ні відео, ні фото за ним — можливо, контент видалено або недоступний.",
    },
    "tiktok_processing": {
        "be": "Апрацоўваю спасылку на TikTok",
        "en": "Processing the TikTok link",
        "es": "Procesando el enlace de TikTok",
        "kk": "TikTok сілтемесі өңделуде",
        "ru": "Обрабатываю ссылку на TikTok",
        "uk": "Обробляю посилання на TikTok",
    },
    "tiktok_fetch_fail": {
        "be": "Не атрымалася атрымаць відэа па гэтай спасылцы — магчыма, яно прыватнае, выдаленае, заблакаванае па рэгіёне або спасылка бітая.",
        "en": "Couldn't fetch the video for this link — it may be private, deleted, region-blocked or the link is broken.",
        "es": "No pude obtener el video de este enlace — quizás sea privado, esté eliminado, bloqueado por región o el enlace esté roto.",
        "kk": "Бұл сілтеме бойынша видеоны алу сәтсіз аяқталды — жеке, жойылған, аймақ бойынша бұғатталған болуы немесе сілтеме жарамсыз болуы мүмкін.",
        "ru": "Не удалось получить видео по этой ссылке — возможно, оно приватное, удалено, заблокировано по региону или ссылка битая.",
        "uk": "Не вдалося отримати відео за цим посиланням — можливо, воно приватне, видалене, заблоковане за регіоном або посилання бите.",
    },
    "tiktok_generic_fail": {
        "be": "Не атрымалася спампаваць гэта відэа ці слайдшоў з TikTok. Паспрабуй іншую спасылку або паўтары крыху пазней.",
        "en": "Couldn't download this video or slideshow from TikTok. Try another link or retry a bit later.",
        "es": "No pude descargar este video o presentación de TikTok. Prueba otro enlace o inténtalo un poco más tarde.",
        "kk": "Бұл видео немесе слайдшоуды TikTok-тан жүктеу сәтсіз аяқталды. Басқа сілтеме көріңіз немесе біраздан кейін қайталаңыз.",
        "ru": "Не получилось скачать это видео или слайдшоу из TikTok. Попробуй другую ссылку или повтори чуть позже.",
        "uk": "Не вдалося завантажити це відео чи слайдшоу з TikTok. Спробуй інше посилання або повтори трохи пізніше.",
    },
    "tiktok_author": {
        "be": "Аўтар TikTok",
        "en": "TikTok author",
        "es": "Autor de TikTok",
        "kk": "TikTok авторы",
        "ru": "Автор TikTok",
        "uk": "Автор TikTok",
    },
    "tiktok_music": {
        "be": "Музыка з TikTok",
        "en": "Music from TikTok",
        "es": "Música de TikTok",
        "kk": "TikTok музыкасы",
        "ru": "Музыка из TikTok",
        "uk": "Музика з TikTok",
    },
    # ── Статусы ──
    "status_generating_image": {
        "be": "Генерую малюнак",
        "en": "Generating image",
        "es": "Generando imagen",
        "kk": "Сурет генерациялануда",
        "ru": "Генерирую изображение",
        "uk": "Генерую зображення",
    },
    "status_voicing": {
        "be": "Агучваю тэкст",
        "en": "Voicing text",
        "es": "Convirtiendo texto en voz",
        "kk": "Мәтін дауысталуда",
        "ru": "Озвучиваю текст",
        "uk": "Озвучую текст",
    },
    "status_taking_longer": {
        "be": "Гэта зойме крыху больш часу…",
        "en": "This will take a little longer…",
        "es": "Esto tardará un poco más…",
        "kk": "Бұл сәл ұзағырақ уақыт алады…",
        "ru": "Это займёт немного больше времени…",
        "uk": "Це займе трохи більше часу…",
    },
    "status_listening": {
        "be": "Слухаю.",
        "en": "Listening.",
        "es": "Escucho.",
        "kk": "Тыңдап тұрмын.",
        "ru": "Слушаю.",
        "uk": "Слухаю.",
    },
    # ── Кнопки-уточнения: служебные реплики ──
    "pick_suffix": {
        "be": "\n\n(або проста напішы тэкстам)",
        "en": "\n\n(or just write it in text)",
        "es": "\n\n(o simplemente escríbelo en texto)",
        "kk": "\n\n(немесе жай мәтінмен жаз)",
        "ru": "\n\n(или просто напиши текстом)",
        "uk": "\n\n(або просто напиши текстом)",
    },
    "pick_expired": {
        "be": "Кнопкі пратухлі — напішы тэкстам.",
        "en": "Buttons expired — write it in text.",
        "es": "Botones caducados — escríbelo en texto.",
        "kk": "Батырмалардың мерзімі өтті — мәтінмен жаз.",
        "ru": "Кнопки протухли — напиши текстом.",
        "uk": "Кнопки протухли — напиши текстом.",
    },
    "pick_not_yours": {
        "be": "Гэта не твае кнопкі.",
        "en": "These aren't your buttons.",
        "es": "Estos no son tus botones.",
        "kk": "Бұл сенің батырмаларың емес.",
        "ru": "Это не твои кнопки.",
        "uk": "Це не твої кнопки.",
    },
    "pick_choice": {
        "be": "Выбар: {choice}",
        "en": "Choice: {choice}",
        "es": "Elección: {choice}",
        "kk": "Таңдау: {choice}",
        "ru": "Выбор: {choice}",
        "uk": "Вибір: {choice}",
    },
    # ── Владелец ──
    "owner_quota_notice": {
        "be": "⚠️ Квота Gemini вычарпана цалкам па ўсіх мадэлях у маршруце (гл. /stats для дэталяў).",
        "en": "⚠️ Gemini quota fully exhausted across all route models (see /stats for details).",
        "es": "⚠️ Cuota de Gemini agotada por completo en todos los modelos de la ruta (ver /stats para detalles).",
        "kk": "⚠️ Маршруттағы барлық модель бойынша Gemini квотасы толық таусылды (егжей-тегжей /stats).",
        "ru": "⚠️ Квота Gemini исчерпана целиком по всем моделям в маршруте (см. /stats для деталей).",
        "uk": "⚠️ Квоту Gemini вичерпано цілком за всіма моделями в маршруті (див. /stats для деталей).",
    },
}

# ── Языковые паки волны 1 (pt/ar/tr/de/fr/it) ──
# Формат: PACK_XX[key] = текст. Реестр — в LANG_PACKS ниже.
PACK_PT: dict[str, str] = {
    "lang_title": "Escolha o idioma do bot:",
    "lang_done": "Idioma: Português.",
    "lang_deny": "Em grupos, apenas um administrador, o criador do grupo ou o dono do bot podem mudar o idioma. Em mensagens diretas, está disponível para todos.",
    "cmd_desc_start": "Sobre o bot e lista de comandos",
    "cmd_desc_reset": "Limpar o histórico do diálogo",
    "cmd_desc_draw": "Desenhar uma imagem a partir de uma descrição",
    "cmd_desc_tts": "Converter texto em voz",
    "cmd_desc_lang": "Idioma do bot",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Respondo perguntas (com busca na web quando preciso), leio sites e vídeos do YouTube por link, analiso fotos, vídeos, áudios e documentos, desenho imagens a partir de descrições e converto texto em voz.\n\n"
        "<b>Comandos</b>\n"
        "/draw [descrição] — desenhar uma imagem\n"
        "/tts [texto] — converter texto em voz\n"
        "/reset — limpar o histórico do diálogo\n"
        "/lang — idioma do bot\n\n"
        "Também dá para desenhar e dar voz só com palavras, sem comandos — por exemplo “desenhe um gato” ou “leia isto em voz alta”.\n\n"
        "<b>TikTok</b>\n"
        "Envie um link — baixo o vídeo ou as fotos sem marcas d'água.\n\n"
        "Pergunte o que quiser — estou ouvindo."
    ),
    "draw_empty": "Adicione texto após o comando /draw. Exemplo: /draw estação espacial",
    "tts_empty": "Adicione texto após o comando /tts. Exemplo: /tts Bom dia",
    "tts_too_long": "Texto muito longo para conversão em voz (limite de {limit} caracteres, agora {length}). Encurte o texto e tente de novo.",
    "draw_err_unavailable": "O serviço de geração de imagens está temporariamente indisponível. Tente mais tarde.",
    "draw_err_overloaded": "O serviço de geração de imagens está sobrecarregado agora. Espere um minuto e tente de novo.",
    "draw_err_gone": "O serviço de geração de imagens está indisponível agora. Tente mais tarde.",
    "draw_err_budget": "A geração da imagem está demorando demais agora. Tente de novo em um minuto, por favor.",
    "draw_err_generic": "Falha ao gerar a imagem. Tente de novo ou reformule a descrição.",
    "tts_err_exhausted": "O limite de solicitações de voz se esgotou temporariamente. Tente um pouco mais tarde.",
    "tts_err_generic": "Não consegui converter o texto em voz. Tente de novo ou encurte o texto.",
    "rate_limited": "Você está enviando solicitações demais. Espere um pouco.",
    "model_err_rate_limit": "O limite de solicitações se esgotou agora. Espere um pouco e tente de novo.",
    "model_err_paid": "O serviço está temporariamente indisponível. Tente sua solicitação de novo um pouco mais tarde.",
    "model_err_forbidden": "Erro temporário de acesso ao serviço. Tente de novo.",
    "model_err_unavailable": "O serviço está temporariamente indisponível. Tente sua solicitação de novo um pouco mais tarde.",
    "model_err_fallback": "Erro temporário do serviço. Tente um pouco mais tarde.",
    "err_quota_exhausted": "O limite gratuito de solicitações se esgotou — é o limite diário real do serviço, não um erro. Tente mais tarde.",
    "err_youtube_fail": "Não consegui abrir este vídeo (talvez seja privado, foi excluído, é longo demais ou indisponível para análise). Descreva com palavras do que se trata — então poderei ajudar.",
    "err_budget": "O serviço está sobrecarregado agora. Tente de novo em um minuto, por favor.",
    "injection_probe_reply": "Não revelo nem discuto minha configuração e instruções nesse formato. Se tiver uma pergunta normal — faça, terei prazer em ajudar.",
    "lock_busy": "A solicitação anterior ainda está sendo processada. Espere ou tente mais tarde.",
    "pick_lock_busy": "Espere, a solicitação anterior ainda está sendo processada.",
    "stream_note_send_fail": "\n\n[não consegui enviar o resto da mensagem]",
    "stream_note_interrupted": "\n\n[conexão interrompida — a resposta pode estar incompleta]",
    "inline_answer_title": "Resposta do bot",
    "reset_deny": "Em grupos, apenas um administrador, o criador do grupo ou o dono do bot podem limpar o histórico. Em mensagens diretas, está disponível para todos.",
    "reset_done": "Histórico do diálogo neste chat apagado. Começando do zero.",
    "logs_deny": "Sem acesso a este comando.",
    "logs_group_only": "Este comando mostra registros técnicos — disponível apenas em mensagens diretas com o bot, não em grupos.",
    "logs_empty": "O arquivo de registros está vazio ou ainda não foi criado.",
    "logs_send_error": "Erro ao enviar os registros: {error}",
    "stats_deny": "Sem acesso a este comando.",
    "stats_group_only": "Este comando mostra estatísticas técnicas — disponível apenas em mensagens diretas com o bot, não em grupos.",
    "stats_no_data": "  sem dados",
    "stats_limit_used": " (limite esgotado)",
    "tiktok_sound_recognized": "Link de som do TikTok reconhecido.",
    "tiktok_sound_no_separate": "Não dá para baixar o som separadamente pelo link da página dele — nem o TikWM nem o próprio TikTok dão a este bot os dados necessários para esse tipo de link. Envie, por favor, um link para qualquer vídeo com este som — o bot enviará o som junto com ele.",
    "tiktok_dl_hd": "Baixando vídeo sem marcas d'água (HD)",
    "tiktok_dl_as_is": "Não encontrei versão sem marcas d'água — baixando como está",
    "tiktok_dl_plain": "Baixando vídeo sem marcas d'água",
    "tiktok_too_big": "Este vídeo do TikTok é grande demais para enviar — a API de bots do Telegram limita o envio de arquivos a 50 MB. Tente baixar este vídeo de outro jeito.",
    "tiktok_no_media": "Link reconhecido, mas o TikTok não devolveu nem vídeo nem fotos — talvez o conteúdo tenha sido excluído ou esteja indisponível.",
    "tiktok_processing": "Processando o link do TikTok",
    "tiktok_fetch_fail": "Não consegui obter o vídeo deste link — talvez seja privado, foi excluído, bloqueado por região ou o link está quebrado.",
    "tiktok_generic_fail": "Não consegui baixar este vídeo ou apresentação do TikTok. Tente outro link ou repita um pouco mais tarde.",
    "tiktok_author": "Autor do TikTok",
    "tiktok_music": "Música do TikTok",
    "status_generating_image": "Gerando imagem",
    "status_voicing": "Convertendo texto em voz",
    "status_taking_longer": "Isso vai demorar um pouco mais…",
    "status_listening": "Ouvindo.",
    "pick_suffix": "\n\n(ou simplesmente escreva em texto)",
    "pick_expired": "Botões expirados — escreva em texto.",
    "pick_not_yours": "Estes não são seus botões.",
    "pick_choice": "Escolha: {choice}",
    "owner_quota_notice": "⚠️ Cota do Gemini totalmente esgotada em todos os modelos da rota (ver /stats para detalhes).",
}

PACK_AR: dict[str, str] = {
    "lang_title": "اختر لغة البوت:",
    "lang_done": "اللغة: العربية.",
    "lang_deny": "في المجموعات، فقط المشرف أو منشئ المجموعة أو مالك البوت يمكنه تغيير اللغة. في الرسائل الخاصة، متاح للجميع.",
    "cmd_desc_start": "عن البوت وقائمة الأوامر",
    "cmd_desc_reset": "مسح سجل الحوار",
    "cmd_desc_draw": "رسم صورة من وصف",
    "cmd_desc_tts": "تحويل النص إلى صوت",
    "cmd_desc_lang": "لغة البوت",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "أجيب عن الأسئلة (مع البحث في الإنترنت عند الحاجة)، وأقرأ المواقع وفيديوهات YouTube عبر الرابط، وأحلل الصور والفيديو والصوت والمستندات، وأرسم الصور من الوصف وأحوّل النص إلى صوت.\n\n"
        "<b>الأوامر</b>\n"
        "/draw [وصف] — رسم صورة\n"
        "/tts [نص] — تحويل النص إلى صوت\n"
        "/reset — مسح سجل الحوار\n"
        "/lang — لغة البوت\n\n"
        "يمكنك أيضًا الرسم والتحويل إلى صوت بالكلمات فقط دون أوامر — مثلًا «ارسم قطة» أو «اقرأ هذا بصوت عالٍ».\n\n"
        "<b>TikTok</b>\n"
        "أرسل رابطًا — سأحمّل الفيديو أو الصور دون علامات مائية.\n\n"
        "اسأل ما تشاء — أنا أستمع."
    ),
    "draw_empty": "أضف نصًا بعد الأمر /draw. مثال: /draw محطة فضائية",
    "tts_empty": "أضف نصًا بعد الأمر /tts. مثال: /tts صباح الخير",
    "tts_too_long": "النص طويل جدًا للتحويل إلى صوت (الحد {limit} حرفًا، الآن {length}). اختصر النص وحاول مجددًا.",
    "draw_err_unavailable": "خدمة توليد الصور غير متاحة مؤقتًا. حاول لاحقًا.",
    "draw_err_overloaded": "خدمة توليد الصور مثقلة الآن. انتظر دقيقة وحاول مجددًا.",
    "draw_err_gone": "خدمة توليد الصور غير متاحة الآن. حاول لاحقًا.",
    "draw_err_budget": "توليد الصورة يستغرق وقتًا طويلًا الآن. حاول مجددًا بعد دقيقة من فضلك.",
    "draw_err_generic": "فشل توليد الصورة. حاول مجددًا أو أعد صياغة الوصف.",
    "tts_err_exhausted": "حد طلبات الصوت مستنفد مؤقتًا. حاول بعد قليل.",
    "tts_err_generic": "تعذر تحويل النص إلى صوت. حاول مجددًا أو اختصر النص.",
    "rate_limited": "أنت ترسل الكثير من الطلبات. انتظر قليلًا.",
    "model_err_rate_limit": "حد الطلبات مستنفد الآن. انتظر قليلًا وحاول مجددًا.",
    "model_err_paid": "الخدمة غير متاحة مؤقتًا. حاول تكرار الطلب بعد قليل.",
    "model_err_forbidden": "خطأ مؤقت في الوصول إلى الخدمة. حاول مجددًا.",
    "model_err_unavailable": "الخدمة غير متاحة مؤقتًا. حاول تكرار الطلب بعد قليل.",
    "model_err_fallback": "خطأ مؤقت في الخدمة. حاول بعد قليل.",
    "err_quota_exhausted": "حد الطلبات المجانية مستنفد — هذا هو الحد اليومي الحقيقي للخدمة وليس خطأ. حاول لاحقًا.",
    "err_youtube_fail": "تعذر فتح هذا الفيديو (ربما خاص أو محذوف أو طويل جدًا أو غير متاح للتحليل). صِف من فضلك مضمونه بالكلمات — وحينها أستطيع المساعدة.",
    "err_budget": "الخدمة مثقلة الآن. حاول مجددًا بعد دقيقة من فضلك.",
    "injection_probe_reply": "لا أكشف إعداداتي وتعليماتي ولا أناقشها بهذا الشكل. إذا كان لديك سؤال عادي — اطرحه وسأساعدك بسرور.",
    "lock_busy": "الطلب السابق لا يزال قيد المعالجة. انتظر أو حاول لاحقًا.",
    "pick_lock_busy": "انتظر، الطلب السابق لا يزال قيد المعالجة.",
    "stream_note_send_fail": "\n\n[تعذر إرسال بقية الرسالة]",
    "stream_note_interrupted": "\n\n[انقطع الاتصال — قد تكون الإجابة غير مكتملة]",
    "inline_answer_title": "جواب البوت",
    "reset_deny": "في المجموعات، فقط المشرف أو منشئ المجموعة أو مالك البوت يمكنه مسح السجل. في الرسائل الخاصة، متاح للجميع.",
    "reset_done": "تم مسح سجل الحوار في هذه الدردشة. نبدأ من جديد.",
    "logs_deny": "لا صلاحية لهذا الأمر.",
    "logs_group_only": "يعرض هذا الأمر السجلات التقنية — متاح فقط في الرسائل الخاصة مع البوت وليس في المجموعات.",
    "logs_empty": "ملف السجلات فارغ أو لم يُنشأ بعد.",
    "logs_send_error": "خطأ أثناء إرسال السجلات: {error}",
    "stats_deny": "لا صلاحية لهذا الأمر.",
    "stats_group_only": "يعرض هذا الأمر الإحصاءات التقنية — متاح فقط في الرسائل الخاصة مع البوت وليس في المجموعات.",
    "stats_no_data": "  لا بيانات",
    "stats_limit_used": " (الحد مستنفد)",
    "tiktok_sound_recognized": "تم التعرف على رابط صوت TikTok.",
    "tiktok_sound_no_separate": "تعذر تنزيل الصوت منفصلًا عبر رابط صفحته — لا TikWM ولا TikTok نفسه يمنحان هذا البوت البيانات اللازمة لهذا النوع من الروابط. أرسل من فضلك رابط أي فيديو بهذا الصوت — وسيرسل البوت الصوت معه.",
    "tiktok_dl_hd": "جارٍ تنزيل الفيديو دون علامات مائية (HD)",
    "tiktok_dl_as_is": "لم توجد نسخة دون علامات مائية — جارٍ التنزيل كما هو",
    "tiktok_dl_plain": "جارٍ تنزيل الفيديو دون علامات مائية",
    "tiktok_too_big": "هذا الفيديو من TikTok كبير جدًا للإرسال — واجهة Telegram Bot API تحدّ تحميل الملفات بـ 50 م.ب. حاول تنزيل الفيديو بطريقة أخرى.",
    "tiktok_no_media": "تم التعرف على الرابط لكن TikTok لم يعطِ فيديو ولا صور — ربما المحتوى محذوف أو غير متاح.",
    "tiktok_processing": "جارٍ معالجة رابط TikTok",
    "tiktok_fetch_fail": "تعذر جلب الفيديو لهذا الرابط — ربما خاص أو محذوف أو محظور إقليميًا أو الرابط تالف.",
    "tiktok_generic_fail": "تعذر تنزيل هذا الفيديو أو عرض الشرائح من TikTok. جرب رابطًا آخر أو أعد المحاولة بعد قليل.",
    "tiktok_author": "مؤلف TikTok",
    "tiktok_music": "موسيقى من TikTok",
    "status_generating_image": "جارٍ توليد الصورة",
    "status_voicing": "جارٍ تحويل النص إلى صوت",
    "status_taking_longer": "سيستغرق هذا وقتًا أطول قليلًا…",
    "status_listening": "أستمع.",
    "pick_suffix": "\n\n(أو اكتبها نصًا فقط)",
    "pick_expired": "انتهت صلاحية الأزرار — اكتبها نصًا.",
    "pick_not_yours": "هذه ليست أزرارك.",
    "pick_choice": "الاختيار: {choice}",
    "owner_quota_notice": "⚠️ حصة Gemini مستنفدة بالكامل عبر كل نماذج المسار (انظر /stats للتفاصيل).",
}

PACK_TR: dict[str, str] = {
    "lang_title": "Bot dilini seç:",
    "lang_done": "Dil: Türkçe.",
    "lang_deny": "Gruplarda dili yalnızca yönetici, grup kurucusu veya bot sahibi değiştirebilir. Direkt mesajlarda herkese açıktır.",
    "cmd_desc_start": "Bot hakkında ve komut listesi",
    "cmd_desc_reset": "Sohbet geçmişini temizle",
    "cmd_desc_draw": "Açıklamadan görsel oluştur",
    "cmd_desc_tts": "Metni seslendir",
    "cmd_desc_lang": "Bot dili",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Soruları yanıtlarım (gerekirse internette arayarak), bağlantıdan siteleri ve YouTube videolarını okurum, fotoğraf, video, ses ve belgeleri incelerim, açıklamadan görsel çizerim ve metni seslendiririm.\n\n"
        "<b>Komutlar</b>\n"
        "/draw [açıklama] — görsel çiz\n"
        "/tts [metin] — metni seslendir\n"
        "/reset — sohbet geçmişini temizle\n"
        "/lang — bot dili\n\n"
        "Komutsuz, düz sözle de çizip seslendirebilirsin — örneğin “kedi çiz” veya “şunu seslendir”.\n\n"
        "<b>TikTok</b>\n"
        "Bağlantı gönder — videoyu veya fotoğrafları filigransız indireyim.\n\n"
        "Ne istersen sor — dinliyorum."
    ),
    "draw_empty": "/draw komutundan sonra metin yaz. Örnek: /draw uzay istasyonu",
    "tts_empty": "/tts komutundan sonra metin yaz. Örnek: /tts Günaydın",
    "tts_too_long": "Metin seslendirme için çok uzun (limit {limit} karakter, şimdi {length}). Metni kısaltıp tekrar dene.",
    "draw_err_unavailable": "Görsel oluşturma servisi geçici olarak kullanılamıyor. Daha sonra dene.",
    "draw_err_overloaded": "Görsel oluşturma servisi şu an aşırı yüklü. Bir dakika bekleyip tekrar dene.",
    "draw_err_gone": "Görsel oluşturma servisi şu an kullanılamıyor. Daha sonra dene.",
    "draw_err_budget": "Görsel oluşturma şu an çok uzun sürüyor. Lütfen bir dakika sonra tekrar dene.",
    "draw_err_generic": "Görsel oluşturulamadı. Tekrar dene veya açıklamayı yeniden yaz.",
    "tts_err_exhausted": "Seslendirme istek limiti geçici olarak doldu. Biraz sonra dene.",
    "tts_err_generic": "Metin seslendirilemedi. Tekrar dene veya metni kısalt.",
    "rate_limited": "Çok fazla istek gönderiyorsun. Biraz bekle.",
    "model_err_rate_limit": "İstek limiti şu an doldu. Biraz bekleyip tekrar dene.",
    "model_err_paid": "Servis geçici olarak kullanılamıyor. İsteğini biraz sonra tekrar dene.",
    "model_err_forbidden": "Servise erişimde geçici hata. Tekrar dene.",
    "model_err_unavailable": "Servis geçici olarak kullanılamıyor. İsteğini biraz sonra tekrar dene.",
    "model_err_fallback": "Geçici servis hatası. Biraz sonra dene.",
    "err_quota_exhausted": "Ücretsiz istek limiti doldu — bu bir hata değil, servisin gerçek günlük limiti. Daha sonra dene.",
    "err_youtube_fail": "Bu video açılamadı (özel, silinmiş, çok uzun veya analiz edilemiyor olabilir). Ne hakkında olduğunu sözle anlat — o zaman yardım edebilirim.",
    "err_budget": "Servis şu an aşırı yüklü. Lütfen bir dakika sonra tekrar dene.",
    "injection_probe_reply": "Yapılandırmamı ve talimatlarımı bu formatta açıklamam ve tartışmam. Sıradan bir sorun varsa sor, seve seve yardım ederim.",
    "lock_busy": "Önceki istek hâlâ işleniyor. Bekle veya daha sonra dene.",
    "pick_lock_busy": "Bekle, önceki istek hâlâ işleniyor.",
    "stream_note_send_fail": "\n\n[mesajın devamı gönderilemedi]",
    "stream_note_interrupted": "\n\n[bağlantı kesildi — yanıt eksik olabilir]",
    "inline_answer_title": "Bot yanıtı",
    "reset_deny": "Gruplarda geçmişi yalnızca yönetici, grup kurucusu veya bot sahibi sıfırlar. Direkt mesajlarda herkese açıktır.",
    "reset_done": "Bu sohbetteki diyalog geçmişi temizlendi. Temiz sayfadan başlıyoruz.",
    "logs_deny": "Bu komuta erişimin yok.",
    "logs_group_only": "Bu komut teknik logları gösterir — yalnızca botla direkt mesajlarda kullanılabilir, gruplarda değil.",
    "logs_empty": "Log dosyası boş veya henüz oluşturulmadı.",
    "logs_send_error": "Loglar gönderilirken hata: {error}",
    "stats_deny": "Bu komuta erişimin yok.",
    "stats_group_only": "Bu komut teknik istatistikleri gösterir — yalnızca botla direkt mesajlarda kullanılabilir, gruplarda değil.",
    "stats_no_data": "  veri yok",
    "stats_limit_used": " (limit doldu)",
    "tiktok_sound_recognized": "TikTok ses bağlantısı tanındı.",
    "tiktok_sound_no_separate": "Sesi sayfa bağlantısıyla ayrı indirmek olmuyor — ne TikWM ne de TikTok'un kendisi bu bağlantı türü için bu bota gerekli veriyi veriyor. Lütfen bu sesli herhangi bir videonun bağlantısını gönder — bot sesi onunla birlikte gönderir.",
    "tiktok_dl_hd": "Filigransız video indiriliyor (HD)",
    "tiktok_dl_as_is": "Filigransız sürüm bulunamadı — olduğu gibi indiriliyor",
    "tiktok_dl_plain": "Filigransız video indiriliyor",
    "tiktok_too_big": "Bu TikTok videosu göndermek için çok büyük — Telegram Bot API dosya yüklemeyi 50 MB ile sınırlıyor. Videoyu başka yolla indirmeyi dene.",
    "tiktok_no_media": "Bağlantı tanındı ama TikTok ne video ne fotoğraf verdi — içerik silinmiş veya kullanılamıyor olabilir.",
    "tiktok_processing": "TikTok bağlantısı işleniyor",
    "tiktok_fetch_fail": "Bu bağlantıdan video alınamadı — özel, silinmiş, bölge engelli olabilir veya bağlantı bozuktur.",
    "tiktok_generic_fail": "Bu video veya slayt TikTok'tan indirilemedi. Başka bağlantı dene veya biraz sonra tekrarla.",
    "tiktok_author": "TikTok yazarı",
    "tiktok_music": "TikTok müziği",
    "status_generating_image": "Görsel oluşturuluyor",
    "status_voicing": "Metin seslendiriliyor",
    "status_taking_longer": "Bu biraz daha uzun sürecek…",
    "status_listening": "Dinliyorum.",
    "pick_suffix": "\n\n(veya düz metinle yaz)",
    "pick_expired": "Düğmelerin süresi doldu — metinle yaz.",
    "pick_not_yours": "Bunlar senin düğmelerin değil.",
    "pick_choice": "Seçim: {choice}",
    "owner_quota_notice": "⚠️ Gemini kotası rotadaki tüm modellerde tamamen doldu (detaylar için /stats).",
}

PACK_DE: dict[str, str] = {
    "lang_title": "Bot-Sprache wählen:",
    "lang_done": "Sprache: Deutsch.",
    "lang_deny": "In Gruppen können nur ein Admin, der Gruppenersteller oder der Bot-Besitzer die Sprache ändern. In Direktnachrichten ist sie für alle verfügbar.",
    "cmd_desc_start": "Über den Bot und Befehlsliste",
    "cmd_desc_reset": "Dialogverlauf löschen",
    "cmd_desc_draw": "Bild aus Beschreibung erzeugen",
    "cmd_desc_tts": "Text vertonen",
    "cmd_desc_lang": "Bot-Sprache",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Ich beantworte Fragen (bei Bedarf mit Websuche), lese Websites und YouTube-Videos per Link, analysiere Fotos, Videos, Audio und Dokumente, zeichne Bilder aus Beschreibungen und vertone Text.\n\n"
        "<b>Befehle</b>\n"
        "/draw [Beschreibung] — Bild zeichnen\n"
        "/tts [Text] — Text vertonen\n"
        "/reset — Dialogverlauf löschen\n"
        "/lang — Bot-Sprache\n\n"
        "Zeichnen und Vertonen geht auch einfach mit Worten, ohne Befehle — z. B. „zeichne eine Katze“ oder „lies das vor“.\n\n"
        "<b>TikTok</b>\n"
        "Link schicken — ich lade Video oder Fotos ohne Wasserzeichen herunter.\n\n"
        "Frag einfach — ich höre zu."
    ),
    "draw_empty": "Text nach dem Befehl /draw angeben. Beispiel: /draw Raumstation",
    "tts_empty": "Text nach dem Befehl /tts angeben. Beispiel: /tts Guten Tag",
    "tts_too_long": "Text ist zu lang zum Vertonen (Limit {limit} Zeichen, jetzt {length}). Text kürzen und erneut versuchen.",
    "draw_err_unavailable": "Bilderzeugung ist vorübergehend nicht verfügbar. Später versuchen.",
    "draw_err_overloaded": "Bilderzeugung ist gerade überlastet. Eine Minute warten und erneut versuchen.",
    "draw_err_gone": "Bilderzeugung ist gerade nicht verfügbar. Später versuchen.",
    "draw_err_budget": "Bilderzeugung dauert gerade zu lange. Bitte in einer Minute erneut versuchen.",
    "draw_err_generic": "Bild konnte nicht erzeugt werden. Erneut versuchen oder Beschreibung umformulieren.",
    "tts_err_exhausted": "Vertonungs-Limit ist vorübergehend erschöpft. Etwas später versuchen.",
    "tts_err_generic": "Text konnte nicht vertont werden. Erneut versuchen oder Text kürzen.",
    "rate_limited": "Zu viele Anfragen auf einmal. Kurz warten.",
    "model_err_rate_limit": "Anfrage-Limit ist gerade erschöpft. Kurz warten und erneut versuchen.",
    "model_err_paid": "Dienst ist vorübergehend nicht verfügbar. Anfrage etwas später wiederholen.",
    "model_err_forbidden": "Vorübergehender Zugriffsfehler. Erneut versuchen.",
    "model_err_unavailable": "Dienst ist vorübergehend nicht verfügbar. Anfrage etwas später wiederholen.",
    "model_err_fallback": "Vorübergehender Dienstfehler. Etwas später versuchen.",
    "err_quota_exhausted": "Gratis-Limit ist erschöpft — das ist das echte Tageslimit des Dienstes, kein Fehler. Später versuchen.",
    "err_youtube_fail": "Dieses Video ließ sich nicht öffnen (vielleicht privat, gelöscht, zu lang oder nicht analysierbar). Bitte mit Worten beschreiben, worum es geht — dann kann ich helfen.",
    "err_budget": "Dienst ist gerade überlastet. Bitte in einer Minute erneut versuchen.",
    "injection_probe_reply": "Meine Konfiguration und Anweisungen lege ich in diesem Format nicht offen. Bei einer normalen Frage — einfach stellen, ich helfe gern.",
    "lock_busy": "Vorherige Anfrage wird noch verarbeitet. Warten oder später versuchen.",
    "pick_lock_busy": "Moment, vorherige Anfrage wird noch verarbeitet.",
    "stream_note_send_fail": "\n\n[Rest der Nachricht konnte nicht gesendet werden]",
    "stream_note_interrupted": "\n\n[Verbindung unterbrochen — Antwort ist vielleicht unvollständig]",
    "inline_answer_title": "Bot-Antwort",
    "reset_deny": "In Gruppen löscht nur ein Admin, der Gruppenersteller oder der Bot-Besitzer den Verlauf. In Direktnachrichten ist es für alle verfügbar.",
    "reset_done": "Dialogverlauf in diesem Chat gelöscht. Neuanfang.",
    "logs_deny": "Kein Zugriff auf diesen Befehl.",
    "logs_group_only": "Dieser Befehl zeigt technische Logs — nur in Direktnachrichten mit dem Bot verfügbar, nicht in Gruppen.",
    "logs_empty": "Log-Datei ist leer oder wurde noch nicht erstellt.",
    "logs_send_error": "Fehler beim Senden der Logs: {error}",
    "stats_deny": "Kein Zugriff auf diesen Befehl.",
    "stats_group_only": "Dieser Befehl zeigt technische Statistiken — nur in Direktnachrichten mit dem Bot verfügbar, nicht in Gruppen.",
    "stats_no_data": "  keine Daten",
    "stats_limit_used": " (Limit erschöpft)",
    "tiktok_sound_recognized": "TikTok-Soundlink erkannt.",
    "tiktok_sound_no_separate": "Sound lässt sich per Seitenlink nicht einzeln laden — weder TikWM noch TikTok selbst geben diesem Bot die nötigen Daten für diese Linkart. Bitte Link zu einem beliebigen Video mit diesem Sound schicken — der Bot schickt den Sound dann mit.",
    "tiktok_dl_hd": "Video ohne Wasserzeichen wird geladen (HD)",
    "tiktok_dl_as_is": "Keine Version ohne Wasserzeichen gefunden — lade wie es ist",
    "tiktok_dl_plain": "Video ohne Wasserzeichen wird geladen",
    "tiktok_too_big": "Dieses TikTok-Video ist zu groß zum Senden — die Telegram Bot API begrenzt Datei-Uploads auf 50 MB. Video auf anderem Weg laden.",
    "tiktok_no_media": "Link erkannt, aber TikTok gab weder Video noch Fotos dazu — Inhalt vielleicht gelöscht oder nicht verfügbar.",
    "tiktok_processing": "TikTok-Link wird verarbeitet",
    "tiktok_fetch_fail": "Video zu diesem Link konnte nicht geholt werden — vielleicht privat, gelöscht, regionsgesperrt oder Link defekt.",
    "tiktok_generic_fail": "Dieses Video bzw. diese Slideshow ließ sich nicht von TikTok laden. Anderen Link versuchen oder etwas später wiederholen.",
    "tiktok_author": "TikTok-Autor",
    "tiktok_music": "Musik von TikTok",
    "status_generating_image": "Bild wird erzeugt",
    "status_voicing": "Text wird vertont",
    "status_taking_longer": "Das dauert etwas länger…",
    "status_listening": "Ich höre zu.",
    "pick_suffix": "\n\n(oder einfach als Text schreiben)",
    "pick_expired": "Buttons abgelaufen — als Text schreiben.",
    "pick_not_yours": "Das sind nicht deine Buttons.",
    "pick_choice": "Wahl: {choice}",
    "owner_quota_notice": "⚠️ Gemini-Kontingent über alle Routenmodelle restlos erschöpft (Details siehe /stats).",
}

PACK_FR: dict[str, str] = {
    "lang_title": "Choisis la langue du bot :",
    "lang_done": "Langue : Français.",
    "lang_deny": "Dans les groupes, seuls un admin, le créateur du groupe ou le propriétaire du bot peuvent changer la langue. En messages privés, c'est accessible à tous.",
    "cmd_desc_start": "À propos du bot et liste des commandes",
    "cmd_desc_reset": "Effacer l'historique du dialogue",
    "cmd_desc_draw": "Dessiner une image d'après une description",
    "cmd_desc_tts": "Convertir le texte en voix",
    "cmd_desc_lang": "Langue du bot",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Je réponds aux questions (avec recherche web si besoin), je lis les sites et les vidéos YouTube par lien, j'analyse photos, vidéos, audios et documents, je dessine des images d'après une description et je convertis le texte en voix.\n\n"
        "<b>Commandes</b>\n"
        "/draw [description] — dessiner une image\n"
        "/tts [texte] — convertir le texte en voix\n"
        "/reset — effacer l'historique du dialogue\n"
        "/lang — langue du bot\n\n"
        "On peut aussi dessiner et donner de la voix avec des mots, sans commandes — par exemple «dessine un chat» ou «lis ça à voix haute».\n\n"
        "<b>TikTok</b>\n"
        "Envoie un lien — je téléchargerai la vidéo ou les photos sans filigrane.\n\n"
        "Demande ce que tu veux — j'écoute."
    ),
    "draw_empty": "Ajoute du texte après la commande /draw. Exemple : /draw station spatiale",
    "tts_empty": "Ajoute du texte après la commande /tts. Exemple : /tts Bonjour",
    "tts_too_long": "Texte trop long pour la voix (limite de {limit} caractères, actuellement {length}). Raccourcis le texte et réessaie.",
    "draw_err_unavailable": "Le service de génération d'images est temporairement indisponible. Réessaie plus tard.",
    "draw_err_overloaded": "Le service de génération d'images est surchargé en ce moment. Attends une minute et réessaie.",
    "draw_err_gone": "Le service de génération d'images est indisponible en ce moment. Réessaie plus tard.",
    "draw_err_budget": "La génération de l'image prend trop de temps en ce moment. Réessaie dans une minute, s'il te plaît.",
    "draw_err_generic": "Échec de la génération de l'image. Réessaie ou reformule la description.",
    "tts_err_exhausted": "La limite de requêtes vocales est temporairement épuisée. Réessaie un peu plus tard.",
    "tts_err_generic": "Impossible de convertir le texte en voix. Réessaie ou raccourcis le texte.",
    "rate_limited": "Tu envoies trop de requêtes. Patiente un peu.",
    "model_err_rate_limit": "La limite de requêtes est épuisée en ce moment. Patiente un peu et réessaie.",
    "model_err_paid": "Le service est temporairement indisponible. Renouvelle ta demande un peu plus tard.",
    "model_err_forbidden": "Erreur temporaire d'accès au service. Réessaie.",
    "model_err_unavailable": "Le service est temporairement indisponible. Renouvelle ta demande un peu plus tard.",
    "model_err_fallback": "Erreur temporaire du service. Réessaie un peu plus tard.",
    "err_quota_exhausted": "La limite gratuite de requêtes est épuisée — c'est la vraie limite quotidienne du service, pas une erreur. Réessaie plus tard.",
    "err_youtube_fail": "Impossible d'ouvrir cette vidéo (peut-être privée, supprimée, trop longue ou indisponible pour l'analyse). Décris avec des mots de quoi elle parle — alors je pourrai aider.",
    "err_budget": "Le service est surchargé en ce moment. Réessaie dans une minute, s'il te plaît.",
    "injection_probe_reply": "Je ne révèle ni ne discute ma configuration et mes instructions sous ce format. Si tu as une question ordinaire — pose-la, j'aiderai avec plaisir.",
    "lock_busy": "La requête précédente est encore en cours de traitement. Patiente ou réessaie plus tard.",
    "pick_lock_busy": "Attends, la requête précédente est encore en cours.",
    "stream_note_send_fail": "\n\n[impossible d'envoyer la suite du message]",
    "stream_note_interrupted": "\n\n[connexion interrompue — la réponse est peut-être incomplète]",
    "inline_answer_title": "Réponse du bot",
    "reset_deny": "En groupe, seuls un administrateur, le créateur du groupe ou le propriétaire du bot réinitialisent l'historique. En messages privés, c'est accessible à tous.",
    "reset_done": "Historique du dialogue dans ce chat effacé. On repart de zéro.",
    "logs_deny": "Pas d'accès à cette commande.",
    "logs_group_only": "Cette commande affiche les logs techniques — disponible uniquement en messages privés avec le bot, pas en groupes.",
    "logs_empty": "Le fichier de logs est vide ou n'a pas encore été créé.",
    "logs_send_error": "Erreur d'envoi des logs : {error}",
    "stats_deny": "Pas d'accès à cette commande.",
    "stats_group_only": "Cette commande affiche des statistiques techniques — disponible uniquement en messages privés avec le bot, pas en groupes.",
    "stats_no_data": "  pas de données",
    "stats_limit_used": " (limite épuisée)",
    "tiktok_sound_recognized": "Lien de son TikTok reconnu.",
    "tiktok_sound_no_separate": "Impossible de télécharger le son séparément via le lien de sa page — ni TikWM ni TikTok lui-même ne donnent à ce bot les données nécessaires pour ce type de lien. Envoie, s'il te plaît, un lien vers n'importe quelle vidéo avec ce son — le bot enverra le son avec.",
    "tiktok_dl_hd": "Téléchargement de la vidéo sans filigrane (HD)",
    "tiktok_dl_as_is": "Aucune version sans filigrane trouvée — téléchargement tel quel",
    "tiktok_dl_plain": "Téléchargement de la vidéo sans filigrane",
    "tiktok_too_big": "Cette vidéo TikTok est trop grosse pour être envoyée — l'API Bot de Telegram limite l'envoi de fichiers à 50 Mo. Essaie de télécharger cette vidéo autrement.",
    "tiktok_no_media": "Lien reconnu, mais TikTok n'a donné ni vidéo ni photos — le contenu est peut-être supprimé ou indisponible.",
    "tiktok_processing": "Traitement du lien TikTok",
    "tiktok_fetch_fail": "Impossible de récupérer la vidéo de ce lien — peut-être privée, supprimée, bloquée par région ou lien cassé.",
    "tiktok_generic_fail": "Impossible de télécharger cette vidéo ou ce diaporama depuis TikTok. Essaie un autre lien ou réessaie un peu plus tard.",
    "tiktok_author": "Auteur TikTok",
    "tiktok_music": "Musique de TikTok",
    "status_generating_image": "Génération de l'image",
    "status_voicing": "Conversion du texte en voix",
    "status_taking_longer": "Ça va prendre un peu plus de temps…",
    "status_listening": "J'écoute.",
    "pick_suffix": "\n\n(ou écris simplement en texte)",
    "pick_expired": "Boutons expirés — écris en texte.",
    "pick_not_yours": "Ce ne sont pas tes boutons.",
    "pick_choice": "Choix : {choice}",
    "owner_quota_notice": "⚠️ Quota Gemini entièrement épuisé sur tous les modèles de la route (voir /stats pour les détails).",
}

PACK_IT: dict[str, str] = {
    "lang_title": "Scegli la lingua del bot:",
    "lang_done": "Lingua: Italiano.",
    "lang_deny": "Nei gruppi solo un admin, il creatore del gruppo o il proprietario del bot possono cambiare la lingua. Nei messaggi diretti è disponibile per tutti.",
    "cmd_desc_start": "Info sul bot ed elenco comandi",
    "cmd_desc_reset": "Cancella la cronologia del dialogo",
    "cmd_desc_draw": "Disegna un'immagine da una descrizione",
    "cmd_desc_tts": "Converti il testo in voce",
    "cmd_desc_lang": "Lingua del bot",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Rispondo alle domande (con ricerca web quando serve), leggo siti e video di YouTube tramite link, analizzo foto, video, audio e documenti, disegno immagini da una descrizione e converto il testo in voce.\n\n"
        "<b>Comandi</b>\n"
        "/draw [descrizione] — disegna un'immagine\n"
        "/tts [testo] — converti il testo in voce\n"
        "/reset — cancella la cronologia del dialogo\n"
        "/lang — lingua del bot\n\n"
        "Si può anche disegnare e dare voce con parole, senza comandi — per esempio “disegna un gatto” o “leggi questo ad alta voce”.\n\n"
        "<b>TikTok</b>\n"
        "Invia un link — scaricherò il video o le foto senza watermark.\n\n"
        "Chiedi pure — ti ascolto."
    ),
    "draw_empty": "Aggiungi testo dopo il comando /draw. Esempio: /draw stazione spaziale",
    "tts_empty": "Aggiungi testo dopo il comando /tts. Esempio: /tts Buongiorno",
    "tts_too_long": "Testo troppo lungo per la voce (limite di {limit} caratteri, ora {length}). Accorcia il testo e riprova.",
    "draw_err_unavailable": "Il servizio di generazione immagini è temporaneamente non disponibile. Riprova più tardi.",
    "draw_err_overloaded": "Il servizio di generazione immagini è sovraccarico ora. Aspetta un minuto e riprova.",
    "draw_err_gone": "Il servizio di generazione immagini non è disponibile ora. Riprova più tardi.",
    "draw_err_budget": "La generazione dell'immagine sta prendendo troppo tempo ora. Riprova tra un minuto, per favore.",
    "draw_err_generic": "Generazione dell'immagine fallita. Riprova o riformula la descrizione.",
    "tts_err_exhausted": "Il limite di richieste vocali è temporaneamente esaurito. Riprova tra poco.",
    "tts_err_generic": "Impossibile convertire il testo in voce. Riprova o accorcia il testo.",
    "rate_limited": "Stai inviando troppe richieste. Aspetta un po'.",
    "model_err_rate_limit": "Il limite di richieste è esaurito ora. Aspetta un po' e riprova.",
    "model_err_paid": "Il servizio è temporaneamente non disponibile. Riprova la richiesta tra poco.",
    "model_err_forbidden": "Errore temporaneo di accesso al servizio. Riprova.",
    "model_err_unavailable": "Il servizio è temporaneamente non disponibile. Riprova la richiesta tra poco.",
    "model_err_fallback": "Errore temporaneo del servizio. Riprova tra poco.",
    "err_quota_exhausted": "Il limite gratuito di richieste è esaurito — è il vero limite giornaliero del servizio, non un errore. Riprova più tardi.",
    "err_youtube_fail": "Non sono riuscito ad aprire questo video (forse privato, eliminato, troppo lungo o non disponibile per l'analisi). Descrivi a parole di cosa tratta — così potrò aiutare.",
    "err_budget": "Il servizio è sovraccarico ora. Riprova tra un minuto, per favore.",
    "injection_probe_reply": "Non rivelo né discuto la mia configurazione e le mie istruzioni in quel formato. Se hai una domanda normale — falla pure, aiuterò volentieri.",
    "lock_busy": "La richiesta precedente è ancora in elaborazione. Aspetta o riprova più tardi.",
    "pick_lock_busy": "Aspetta, la richiesta precedente è ancora in elaborazione.",
    "stream_note_send_fail": "\n\n[impossibile inviare il resto del messaggio]",
    "stream_note_interrupted": "\n\n[connessione interrotta — la risposta potrebbe essere incompleta]",
    "inline_answer_title": "Risposta del bot",
    "reset_deny": "Nel gruppo solo un amministratore, il creatore del gruppo o il proprietario del bot azzerano la cronologia. Nei messaggi diretti è disponibile per tutti.",
    "reset_done": "Cronologia del dialogo in questa chat cancellata. Si ricomincia da zero.",
    "logs_deny": "Nessun accesso a questo comando.",
    "logs_group_only": "Questo comando mostra i log tecnici — disponibile solo nei messaggi diretti con il bot, non nei gruppi.",
    "logs_empty": "Il file di log è vuoto o non è stato ancora creato.",
    "logs_send_error": "Errore di invio dei log: {error}",
    "stats_deny": "Nessun accesso a questo comando.",
    "stats_group_only": "Questo comando mostra statistiche tecniche — disponibile solo nei messaggi diretti con il bot, non nei gruppi.",
    "stats_no_data": "  nessun dato",
    "stats_limit_used": " (limite esaurito)",
    "tiktok_sound_recognized": "Link audio di TikTok riconosciuto.",
    "tiktok_sound_no_separate": "Impossibile scaricare l'audio separatamente tramite il link della sua pagina — né TikWM né TikTok stesso danno a questo bot i dati necessari per quel tipo di link. Invia, per favore, un link a un qualsiasi video con quest'audio — il bot invierà l'audio insieme.",
    "tiktok_dl_hd": "Download del video senza watermark (HD)",
    "tiktok_dl_as_is": "Nessuna versione senza watermark trovata — scarico com'è",
    "tiktok_dl_plain": "Download del video senza watermark",
    "tiktok_too_big": "Questo video TikTok è troppo grande per essere inviato — le Bot API di Telegram limitano l'invio di file a 50 MB. Prova a scaricare questo video in un altro modo.",
    "tiktok_no_media": "Link riconosciuto, ma TikTok non ha dato né video né foto — forse il contenuto è stato eliminato o non è disponibile.",
    "tiktok_processing": "Elaborazione del link TikTok",
    "tiktok_fetch_fail": "Impossibile ottenere il video da questo link — forse privato, eliminato, bloccato per regione o link rotto.",
    "tiktok_generic_fail": "Impossibile scaricare questo video o slideshow da TikTok. Prova un altro link o ripeti tra poco.",
    "tiktok_author": "Autore TikTok",
    "tiktok_music": "Musica di TikTok",
    "status_generating_image": "Generazione dell'immagine",
    "status_voicing": "Conversione del testo in voce",
    "status_taking_longer": "Ci vorrà un po' più di tempo…",
    "status_listening": "Ti ascolto.",
    "pick_suffix": "\n\n(oppure scrivi semplicemente in testo)",
    "pick_expired": "Pulsanti scaduti — scrivi in testo.",
    "pick_not_yours": "Non sono i tuoi pulsanti.",
    "pick_choice": "Scelta: {choice}",
    "owner_quota_notice": "⚠️ Quota Gemini completamente esaurita su tutti i modelli del percorso (vedi /stats per i dettagli).",
}

PACK_HI: dict[str, str] = {
    "lang_title": "बॉट की भाषा चुनें:",
    "lang_done": "भाषा: हिन्दी।",
    "lang_deny": "ग्रुप में भाषा केवल एडमिन, ग्रुप निर्माता या बॉट का मालिक बदल सकता है। डायरेक्ट संदेशों में यह सभी के लिए उपलब्ध है।",
    "cmd_desc_start": "बॉट के बारे में और कमांड सूची",
    "cmd_desc_reset": "संवाद इतिहास साफ़ करें",
    "cmd_desc_draw": "विवरण से चित्र बनाएं",
    "cmd_desc_tts": "टेक्स्ट को आवाज़ दें",
    "cmd_desc_lang": "बॉट की भाषा",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "मैं सवालों के जवाब देता हूं (ज़रूरत पर इंटरनेट पर खोज के साथ), लिंक से वेबसाइटें और YouTube वीडियो पढ़ता हूं, फ़ोटो, वीडियो, ऑडियो और दस्तावेज़ों का विश्लेषण करता हूं, विवरण से चित्र बनाता हूं और टेक्स्ट को आवाज़ देता हूं।\n\n"
        "<b>कमांड</b>\n"
        "/draw [विवरण] — चित्र बनाएं\n"
        "/tts [टेक्स्ट] — टेक्स्ट को आवाज़ दें\n"
        "/reset — संवाद इतिहास साफ़ करें\n"
        "/lang — बॉट की भाषा\n\n"
        "बिना कमांड, सिर्फ शब्दों से भी चित्र बनवा और आवाज़ दिलवा सकते हो — जैसे “बिल्ली बनाओ” या “इसे ज़ोर से पढ़ो”।\n\n"
        "<b>TikTok</b>\n"
        "लिंक भेजो — वीडियो या फ़ोटो बिना वॉटरमार्क डाउनलोड कर दूंगा।\n\n"
        "कुछ भी पूछो — सुन रहा हूं।"
    ),
    "draw_empty": "/draw कमांड के बाद टेक्स्ट लिखो। उदाहरण: /draw अंतरिक्ष स्टेशन",
    "tts_empty": "/tts कमांड के बाद टेक्स्ट लिखो। उदाहरण: /tts नमस्ते",
    "tts_too_long": "टेक्स्ट आवाज़ के लिए बहुत लंबा है (सीमा {limit} अक्षर, अभी {length})। टेक्स्ट छोटा करके फिर कोशिश करो।",
    "draw_err_unavailable": "चित्र बनाने की सेवा अस्थायी रूप से उपलब्ध नहीं है। बाद में कोशिश करो।",
    "draw_err_overloaded": "चित्र बनाने की सेवा अभी अतिभारित है। एक मिनट रुको और फिर कोशिश करो।",
    "draw_err_gone": "चित्र बनाने की सेवा अभी उपलब्ध नहीं है। बाद में कोशिश करो।",
    "draw_err_budget": "चित्र बनाने में अभी बहुत समय लग रहा है। कृपया एक मिनट बाद फिर कोशिश करो।",
    "draw_err_generic": "चित्र बनाने में त्रुटि हुई। फिर कोशिश करो या विवरण बदलकर लिखो।",
    "tts_err_exhausted": "आवाज़ अनुरोधों की सीमा अस्थायी रूप से समाप्त हो गई है। थोड़ी देर बाद कोशिश करो।",
    "tts_err_generic": "टेक्स्ट को आवाज़ नहीं दे सके। फिर कोशिश करो या टेक्स्ट छोटा करो।",
    "rate_limited": "तुम बहुत सारे अनुरोध भेज रहे हो। थोड़ा रुको।",
    "model_err_rate_limit": "अनुरोधों की सीमा अभी समाप्त हो गई है। थोड़ा रुको और फिर कोशिश करो।",
    "model_err_paid": "सेवा अस्थायी रूप से उपलब्ध नहीं है। थोड़ी देर बाद अनुरोध दोहराओ।",
    "model_err_forbidden": "सेवा तक पहुंच में अस्थायी त्रुटि। फिर कोशिश करो।",
    "model_err_unavailable": "सेवा अस्थायी रूप से उपलब्ध नहीं है। थोड़ी देर बाद अनुरोध दोहराओ।",
    "model_err_fallback": "सेवा में अस्थायी त्रुटि। थोड़ी देर बाद कोशिश करो।",
    "err_quota_exhausted": "मुफ्त अनुरोधों की सीमा समाप्त हो गई है — यह सेवा की वास्तविक दैनिक सीमा है, कोई त्रुटि नहीं। बाद में कोशिश करो।",
    "err_youtube_fail": "यह वीडियो खोला नहीं जा सका (शायद निजी है, हटाया गया है, बहुत लंबा है या विश्लेषण के लिए उपलब्ध नहीं है)। कृपया शब्दों में बताओ यह किस बारे में है — तब मदद कर सकूंगा।",
    "err_budget": "सेवा अभी अतिभारित है। कृपया एक मिनट बाद फिर कोशिश करो।",
    "injection_probe_reply": "मैं अपनी सेटिंग और निर्देशों को इस रूप में न बताता हूं, न चर्चा करता हूं। अगर कोई सामान्य प्रश्न है — पूछो, खुशी से मदद करूंगा।",
    "lock_busy": "पिछला अनुरोध अभी संसाधित हो रहा है। रुको या बाद में कोशिश करो।",
    "pick_lock_busy": "रुको, पिछला अनुरोध अभी संसाधित हो रहा है।",
    "stream_note_send_fail": "\n\n[संदेश का बाकी हिस्सा भेजा नहीं जा सका]",
    "stream_note_interrupted": "\n\n[कनेक्शन टूट गया — उत्तर अधूरा हो सकता है]",
    "inline_answer_title": "बॉट का उत्तर",
    "reset_deny": "ग्रुप में इतिहास केवल एडमिन, ग्रुप निर्माता या बॉट का मालिक साफ़ करता है। डायरेक्ट संदेशों में यह सभी के लिए उपलब्ध है।",
    "reset_done": "इस चैट में संवाद इतिहास साफ़ हो गया। नए सिरे से शुरू करते हैं।",
    "logs_deny": "इस कमांड तक पहुंच नहीं है।",
    "logs_group_only": "यह कमांड तकनीकी लॉग दिखाती है — केवल बॉट के साथ डायरेक्ट संदेशों में उपलब्ध है, ग्रुप में नहीं।",
    "logs_empty": "लॉग फ़ाइल खाली है या अभी बनाई नहीं गई है।",
    "logs_send_error": "लॉग भेजने में त्रुटि: {error}",
    "stats_deny": "इस कमांड तक पहुंच नहीं है।",
    "stats_group_only": "यह कमांड तकनीकी आंकड़े दिखाती है — केवल बॉट के साथ डायरेक्ट संदेशों में उपलब्ध है, ग्रुप में नहीं।",
    "stats_no_data": "  कोई डेटा नहीं",
    "stats_limit_used": " (सीमा समाप्त)",
    "tiktok_sound_recognized": "TikTok ध्वनि लिंक पहचानी गई।",
    "tiktok_sound_no_separate": "ध्वनि को उसके पेज के लिंक से अलग डाउनलोड नहीं किया जा सकता — न TikWM न स्वयं TikTok इस तरह के लिंक के लिए इस बॉट को ज़रूरी डेटा देते हैं। कृपया इस ध्वनि वाले किसी भी वीडियो का लिंक भेजें — बॉट ध्वनि उसके साथ भेजेगा।",
    "tiktok_dl_hd": "बिना वॉटरमार्क वीडियो डाउनलोड हो रहा है (HD)",
    "tiktok_dl_as_is": "बिना वॉटरमार्क वाला संस्करण नहीं मिला — जैसा है वैसा डाउनलोड हो रहा है",
    "tiktok_dl_plain": "बिना वॉटरमार्क वीडियो डाउनलोड हो रहा है",
    "tiktok_too_big": "यह TikTok वीडियो भेजने के लिए बहुत बड़ा है — Telegram Bot API फ़ाइल अपलोड को 50 MB तक सीमित करता है। इस वीडियो को किसी और तरीके से डाउनलोड करने की कोशिश करो।",
    "tiktok_no_media": "लिंक पहचाना गया, लेकिन TikTok ने न वीडियो दिया न फ़ोटो — शायद कंटेंट हटाया गया है या उपलब्ध नहीं है।",
    "tiktok_processing": "TikTok लिंक संसाधित हो रहा है",
    "tiktok_fetch_fail": "इस लिंक से वीडियो लाया नहीं जा सका — शायद निजी है, हटाया गया है, क्षेत्र-प्रतिबंधित है या लिंक टूटा है।",
    "tiktok_generic_fail": "यह वीडियो या स्लाइडशो TikTok से डाउनलोड नहीं हो सका। दूसरा लिंक आज़माओ या थोड़ी देर बाद दोहराओ।",
    "tiktok_author": "TikTok लेखक",
    "tiktok_music": "TikTok संगीत",
    "status_generating_image": "चित्र बनाया जा रहा है",
    "status_voicing": "टेक्स्ट को आवाज़ दी जा रही है",
    "status_taking_longer": "इसमें थोड़ा और समय लगेगा…",
    "status_listening": "सुन रहा हूं।",
    "pick_suffix": "\n\n(या बस टेक्स्ट में लिखो)",
    "pick_expired": "बटन समाप्त हो गए — टेक्स्ट में लिखो।",
    "pick_not_yours": "ये तुम्हारे बटन नहीं हैं।",
    "pick_choice": "चयन: {choice}",
    "owner_quota_notice": "⚠️ रूट के सभी मॉडलों में Gemini कोटा पूरी तरह समाप्त (विवरण के लिए /stats)।",
}

PACK_ID: dict[str, str] = {
    "lang_title": "Pilih bahasa bot:",
    "lang_done": "Bahasa: Bahasa Indonesia.",
    "lang_deny": "Di grup, hanya admin, pembuat grup, atau pemilik bot yang bisa mengganti bahasa. Di pesan langsung, tersedia untuk semua orang.",
    "cmd_desc_start": "Tentang bot dan daftar perintah",
    "cmd_desc_reset": "Hapus riwayat dialog",
    "cmd_desc_draw": "Gambar dari deskripsi",
    "cmd_desc_tts": "Ucapkan teks",
    "cmd_desc_lang": "Bahasa bot",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Aku menjawab pertanyaan (dengan pencarian web bila perlu), membaca situs dan video YouTube lewat tautan, menganalisis foto, video, audio dan dokumen, menggambar dari deskripsi, dan mengucapkan teks.\n\n"
        "<b>Perintah</b>\n"
        "/draw [deskripsi] — menggambar\n"
        "/tts [teks] — mengucapkan teks\n"
        "/reset — menghapus riwayat dialog\n"
        "/lang — bahasa bot\n\n"
        "Menggambar dan mengucapkan juga bisa hanya dengan kata-kata, tanpa perintah — misalnya “gambarkan kucing” atau “bacakan ini”.\n\n"
        "<b>TikTok</b>\n"
        "Kirim tautan — akan kuunduh video atau fotonya tanpa watermark.\n\n"
        "Tanya apa saja — aku mendengarkan."
    ),
    "draw_empty": "Tambahkan teks setelah perintah /draw. Contoh: /draw stasiun luar angkasa",
    "tts_empty": "Tambahkan teks setelah perintah /tts. Contoh: /tts Selamat pagi",
    "tts_too_long": "Teks terlalu panjang untuk diucapkan (batas {limit} karakter, sekarang {length}). Persingkat teks dan coba lagi.",
    "draw_err_unavailable": "Layanan pembuatan gambar sementara tidak tersedia. Coba lagi nanti.",
    "draw_err_overloaded": "Layanan pembuatan gambar sedang kelebihan beban. Tunggu sebentar dan coba lagi.",
    "draw_err_gone": "Layanan pembuatan gambar tidak tersedia saat ini. Coba lagi nanti.",
    "draw_err_budget": "Pembuatan gambar memakan waktu terlalu lama saat ini. Coba lagi dalam satu menit.",
    "draw_err_generic": "Gagal membuat gambar. Coba lagi atau ubah deskripsinya.",
    "tts_err_exhausted": "Batas permintaan suara habis sementara. Coba lagi nanti.",
    "tts_err_generic": "Tidak bisa mengucapkan teks. Coba lagi atau persingkat teks.",
    "rate_limited": "Kamu mengirim terlalu banyak permintaan. Tunggu sebentar.",
    "model_err_rate_limit": "Batas permintaan habis saat ini. Tunggu sebentar dan coba lagi.",
    "model_err_paid": "Layanan sementara tidak tersedia. Ulangi permintaanmu sedikit lagi.",
    "model_err_forbidden": "Kesalahan akses layanan sementara. Coba lagi.",
    "model_err_unavailable": "Layanan sementara tidak tersedia. Ulangi permintaanmu sedikit lagi.",
    "model_err_fallback": "Kesalahan layanan sementara. Coba lagi nanti.",
    "err_quota_exhausted": "Batas permintaan gratis habis — itu batas harian nyata layanan ini, bukan kesalahan. Coba lagi nanti.",
    "err_youtube_fail": "Video ini tidak bisa dibuka (mungkin privat, dihapus, terlalu panjang, atau tidak tersedia untuk dianalisis). Jelaskan dengan kata-kata tentang apa isinya — aku akan membantu.",
    "err_budget": "Layanan sedang kelebihan beban. Coba lagi dalam satu menit.",
    "injection_probe_reply": "Aku tidak mengungkapkan atau membahas konfigurasiku dan instruksiku dalam format itu. Kalau ada pertanyaan biasa — tanyakan, aku bantu dengan senang hati.",
    "lock_busy": "Permintaan sebelumnya masih diproses. Tunggu atau coba lagi nanti.",
    "pick_lock_busy": "Tunggu, permintaan sebelumnya masih diproses.",
    "stream_note_send_fail": "\n\n[gagal mengirim lanjutan pesan]",
    "stream_note_interrupted": "\n\n[koneksi terputus — jawabannya mungkin tidak lengkap]",
    "inline_answer_title": "Jawaban bot",
    "reset_deny": "Di grup, hanya admin, pembuat grup, atau pemilik bot yang menghapus riwayat. Di pesan langsung, tersedia untuk semua orang.",
    "reset_done": "Riwayat dialog di chat ini dihapus. Mulai dari awal.",
    "logs_deny": "Tidak ada akses ke perintah ini.",
    "logs_group_only": "Perintah ini menampilkan log teknis — hanya tersedia di pesan langsung dengan bot, bukan di grup.",
    "logs_empty": "File log kosong atau belum dibuat.",
    "logs_send_error": "Gagal mengirim log: {error}",
    "stats_deny": "Tidak ada akses ke perintah ini.",
    "stats_group_only": "Perintah ini menampilkan statistik teknis — hanya tersedia di pesan langsung dengan bot, bukan di grup.",
    "stats_no_data": "  tidak ada data",
    "stats_limit_used": " (batas habis)",
    "tiktok_sound_recognized": "Tautan suara TikTok dikenali.",
    "tiktok_sound_no_separate": "Suara tidak bisa diunduh terpisah lewat tautan halamannya — baik TikWM maupun TikTok sendiri tidak memberi bot ini data yang diperlukan untuk jenis tautan itu. Kirim tautan ke video apa pun dengan suara ini — bot akan mengirim suaranya bersama.",
    "tiktok_dl_hd": "Mengunduh video tanpa watermark (HD)",
    "tiktok_dl_as_is": "Tidak ada versi tanpa watermark — mengunduh apa adanya",
    "tiktok_dl_plain": "Mengunduh video tanpa watermark",
    "tiktok_too_big": "Video TikTok ini terlalu besar untuk dikirim — Bot API Telegram membatasi unggahan file hingga 50 MB. Coba unduh video ini dengan cara lain.",
    "tiktok_no_media": "Tautan dikenali, tetapi TikTok tidak memberi video maupun foto — kontennya mungkin dihapus atau tidak tersedia.",
    "tiktok_processing": "Memproses tautan TikTok",
    "tiktok_fetch_fail": "Tidak bisa mengambil video dari tautan ini — mungkin privat, dihapus, dibatasi wilayah, atau tautannya rusak.",
    "tiktok_generic_fail": "Tidak bisa mengunduh video atau slideshow ini dari TikTok. Coba tautan lain atau ulangi sedikit lagi.",
    "tiktok_author": "Penulis TikTok",
    "tiktok_music": "Musik dari TikTok",
    "status_generating_image": "Membuat gambar",
    "status_voicing": "Mengucapkan teks",
    "status_taking_longer": "Ini akan memakan waktu sedikit lebih lama…",
    "status_listening": "Mendengarkan.",
    "pick_suffix": "\n\n(atau tulis saja sebagai teks)",
    "pick_expired": "Tombol kedaluwarsa — tulis sebagai teks.",
    "pick_not_yours": "Itu bukan tombolmu.",
    "pick_choice": "Pilihan: {choice}",
    "owner_quota_notice": "⚠️ Kuota Gemini habis total di semua model rute (lihat /stats untuk detail).",
}

PACK_MS: dict[str, str] = {
    "lang_title": "Pilih bahasa bot:",
    "lang_done": "Bahasa: Bahasa Melayu.",
    "lang_deny": "Dalam kumpulan, hanya admin, pencipta kumpulan atau pemilik bot boleh menukar bahasa. Dalam mesej langsung, tersedia untuk semua.",
    "cmd_desc_start": "Tentang bot dan senarai perintah",
    "cmd_desc_reset": "Padam sejarah dialog",
    "cmd_desc_draw": "Lukis gambar daripada penerangan",
    "cmd_desc_tts": "Ucapkan teks",
    "cmd_desc_lang": "Bahasa bot",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Saya menjawab soalan (dengan carian web bila perlu), membaca laman dan video YouTube melalui pautan, menganalisis foto, video, audio dan dokumen, melukis gambar daripada penerangan, dan mengucapkan teks.\n\n"
        "<b>Perintah</b>\n"
        "/draw [penerangan] — melukis gambar\n"
        "/tts [teks] — mengucapkan teks\n"
        "/reset — memadam sejarah dialog\n"
        "/lang — bahasa bot\n\n"
        "Melukis dan mengucapkan juga boleh dengan kata-kata sahaja, tanpa perintah — contohnya “lukis kucing” atau “bacakan ini”.\n\n"
        "<b>TikTok</b>\n"
        "Hantar pautan — saya akan muat turun video atau fotonya tanpa tera air.\n\n"
        "Tanya apa sahaja — saya mendengar."
    ),
    "draw_empty": "Tambah teks selepas perintah /draw. Contoh: /draw stesen angkasa",
    "tts_empty": "Tambah teks selepas perintah /tts. Contoh: /tts Selamat pagi",
    "tts_too_long": "Teks terlalu panjang untuk diucapkan (had {limit} aksara, kini {length}). Pendekkan teks dan cuba lagi.",
    "draw_err_unavailable": "Perkhidmatan penjanaan gambar tidak tersedia buat sementara. Cuba lagi nanti.",
    "draw_err_overloaded": "Perkhidmatan penjanaan gambar terlalu sibuk sekarang. Tunggu seminit dan cuba lagi.",
    "draw_err_gone": "Perkhidmatan penjanaan gambar tidak tersedia sekarang. Cuba lagi nanti.",
    "draw_err_budget": "Penjanaan gambar mengambil masa terlalu lama sekarang. Cuba lagi dalam seminit.",
    "draw_err_generic": "Gagal menjana gambar. Cuba lagi atau ubah penerangannya.",
    "tts_err_exhausted": "Had permintaan suara habis buat sementara. Cuba lagi nanti.",
    "tts_err_generic": "Tidak dapat mengucapkan teks. Cuba lagi atau pendekkan teks.",
    "rate_limited": "Awak hantar terlalu banyak permintaan. Tunggu sekejap.",
    "model_err_rate_limit": "Had permintaan habis sekarang. Tunggu sekejap dan cuba lagi.",
    "model_err_paid": "Perkhidmatan tidak tersedia buat sementara. Ulangi permintaan sedikit lagi.",
    "model_err_forbidden": "Ralat akses perkhidmatan sementara. Cuba lagi.",
    "model_err_unavailable": "Perkhidmatan tidak tersedia buat sementara. Ulangi permintaan sedikit lagi.",
    "model_err_fallback": "Ralat perkhidmatan sementara. Cuba lagi nanti.",
    "err_quota_exhausted": "Had permintaan percuma habis — itu had harian sebenar perkhidmatan ini, bukan ralat. Cuba lagi nanti.",
    "err_youtube_fail": "Video ini tidak dapat dibuka (mungkin peribadi, dipadam, terlalu panjang atau tidak tersedia untuk dianalisis). Jelaskan dengan kata-kata tentang apa isinya — saya akan membantu.",
    "err_budget": "Perkhidmatan terlalu sibuk sekarang. Cuba lagi dalam seminit.",
    "injection_probe_reply": "Saya tidak mendedahkan atau membincangkan konfigurasi dan arahan saya dalam format itu. Kalau ada soalan biasa — tanya, saya bantu dengan senang hati.",
    "lock_busy": "Permintaan sebelumnya masih diproses. Tunggu atau cuba lagi nanti.",
    "pick_lock_busy": "Tunggu, permintaan sebelumnya masih diproses.",
    "stream_note_send_fail": "\n\n[gagal menghantar sambungan mesej]",
    "stream_note_interrupted": "\n\n[sambungan terputus — jawapan mungkin tidak lengkap]",
    "inline_answer_title": "Jawapan bot",
    "reset_deny": "Dalam kumpulan, hanya admin, pencipta kumpulan atau pemilik bot memadam sejarah. Dalam mesej langsung, tersedia untuk semua.",
    "reset_done": "Sejarah dialog dalam sembang ini dipadam. Mula semula.",
    "logs_deny": "Tiada akses ke perintah ini.",
    "logs_group_only": "Perintah ini memaparkan log teknikal — hanya tersedia dalam mesej langsung dengan bot, bukan dalam kumpulan.",
    "logs_empty": "Fail log kosong atau belum dibuat.",
    "logs_send_error": "Gagal menghantar log: {error}",
    "stats_deny": "Tiada akses ke perintah ini.",
    "stats_group_only": "Perintah ini memaparkan statistik teknikal — hanya tersedia dalam mesej langsung dengan bot, bukan dalam kumpulan.",
    "stats_no_data": "  tiada data",
    "stats_limit_used": " (had habis)",
    "tiktok_sound_recognized": "Pautan bunyi TikTok dikenal pasti.",
    "tiktok_sound_no_separate": "Bunyi tidak boleh dimuat turun berasingan melalui pautan halamannya — TikWM mahupun TikTok sendiri tidak memberi bot ini data yang diperlukan untuk jenis pautan itu. Hantar pautan ke mana-mana video dengan bunyi ini — bot akan menghantar bunyinya bersama.",
    "tiktok_dl_hd": "Memuat turun video tanpa tera air (HD)",
    "tiktok_dl_as_is": "Tiada versi tanpa tera air — memuat turun seadanya",
    "tiktok_dl_plain": "Memuat turun video tanpa tera air",
    "tiktok_too_big": "Video TikTok ini terlalu besar untuk dihantar — Bot API Telegram mengehadkan muat naik fail hingga 50 MB. Cuba muat turun video ini dengan cara lain.",
    "tiktok_no_media": "Pautan dikenal pasti, tetapi TikTok tidak memberi video mahupun foto — kandungannya mungkin dipadam atau tidak tersedia.",
    "tiktok_processing": "Memproses pautan TikTok",
    "tiktok_fetch_fail": "Tidak dapat mengambil video daripada pautan ini — mungkin peribadi, dipadam, disekat wilayah atau pautannya rosak.",
    "tiktok_generic_fail": "Tidak dapat memuat turun video atau slaid ini dari TikTok. Cuba pautan lain atau ulangi sedikit lagi.",
    "tiktok_author": "Pengarang TikTok",
    "tiktok_music": "Muzik dari TikTok",
    "status_generating_image": "Menjana gambar",
    "status_voicing": "Mengucapkan teks",
    "status_taking_longer": "Ini akan mengambil masa sedikit lebih lama…",
    "status_listening": "Mendengar.",
    "pick_suffix": "\n\n(atau tulis sahaja sebagai teks)",
    "pick_expired": "Butang luput — tulis sebagai teks.",
    "pick_not_yours": "Itu bukan butang awak.",
    "pick_choice": "Pilihan: {choice}",
    "owner_quota_notice": "⚠️ Kuota Gemini habis sepenuhnya di semua model laluan (lihat /stats untuk butiran).",
}

PACK_VI: dict[str, str] = {
    "lang_title": "Chọn ngôn ngữ của bot:",
    "lang_done": "Ngôn ngữ: Tiếng Việt.",
    "lang_deny": "Trong nhóm, chỉ quản trị viên, người tạo nhóm hoặc chủ bot mới đổi được ngôn ngữ. Trong tin nhắn riêng thì ai cũng dùng được.",
    "cmd_desc_start": "Về bot và danh sách lệnh",
    "cmd_desc_reset": "Xóa lịch sử trò chuyện",
    "cmd_desc_draw": "Vẽ ảnh từ mô tả",
    "cmd_desc_tts": "Đọc văn bản thành tiếng",
    "cmd_desc_lang": "Ngôn ngữ của bot",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Mình trả lời câu hỏi (kèm tìm kiếm web khi cần), đọc trang web và video YouTube qua liên kết, phân tích ảnh, video, âm thanh và tài liệu, vẽ ảnh từ mô tả và đọc văn bản thành tiếng.\n\n"
        "<b>Lệnh</b>\n"
        "/draw [mô tả] — vẽ ảnh\n"
        "/tts [văn bản] — đọc văn bản thành tiếng\n"
        "/reset — xóa lịch sử trò chuyện\n"
        "/lang — ngôn ngữ của bot\n\n"
        "Vẽ và đọc thành tiếng cũng được chỉ bằng lời, không cần lệnh — ví dụ “vẽ con mèo” hay “đọc cái này lên”.\n\n"
        "<b>TikTok</b>\n"
        "Gửi liên kết — mình sẽ tải video hoặc ảnh không watermark.\n\n"
        "Hỏi gì cũng được — mình đang nghe."
    ),
    "draw_empty": "Thêm chữ sau lệnh /draw. Ví dụ: /draw trạm vũ trụ",
    "tts_empty": "Thêm chữ sau lệnh /tts. Ví dụ: /tts Chào buổi sáng",
    "tts_too_long": "Chữ quá dài để đọc thành tiếng (giới hạn {limit} ký tự, hiện tại {length}). Rút ngắn chữ rồi thử lại.",
    "draw_err_unavailable": "Dịch vụ tạo ảnh tạm thời không khả dụng. Thử lại sau.",
    "draw_err_overloaded": "Dịch vụ tạo ảnh đang quá tải. Đợi một phút rồi thử lại.",
    "draw_err_gone": "Dịch vụ tạo ảnh hiện không khả dụng. Thử lại sau.",
    "draw_err_budget": "Tạo ảnh đang mất quá lâu. Vui lòng thử lại sau một phút.",
    "draw_err_generic": "Tạo ảnh thất bại. Thử lại hoặc diễn đạt lại mô tả.",
    "tts_err_exhausted": "Hạn mức yêu cầu giọng đọc tạm thời đã hết. Thử lại sau một lúc.",
    "tts_err_generic": "Không đọc được văn bản thành tiếng. Thử lại hoặc rút ngắn chữ.",
    "rate_limited": "Bạn gửi quá nhiều yêu cầu. Đợi một chút.",
    "model_err_rate_limit": "Hạn mức yêu cầu hiện đã hết. Đợi một chút rồi thử lại.",
    "model_err_paid": "Dịch vụ tạm thời không khả dụng. Thử lại yêu cầu sau một lúc.",
    "model_err_forbidden": "Lỗi truy cập dịch vụ tạm thời. Thử lại.",
    "model_err_unavailable": "Dịch vụ tạm thời không khả dụng. Thử lại yêu cầu sau một lúc.",
    "model_err_fallback": "Lỗi dịch vụ tạm thời. Thử lại sau một lúc.",
    "err_quota_exhausted": "Hạn mức yêu cầu miễn phí đã hết — đó là hạn mức hàng ngày thực của dịch vụ, không phải lỗi. Thử lại sau.",
    "err_youtube_fail": "Không mở được video này (có thể riêng tư, đã xóa, quá dài hoặc không phân tích được). Hãy mô tả bằng lời nó nói về gì — mình sẽ giúp.",
    "err_budget": "Dịch vụ đang quá tải. Vui lòng thử lại sau một phút.",
    "injection_probe_reply": "Mình không tiết lộ hay thảo luận cấu hình và hướng dẫn của mình dưới định dạng đó. Nếu có câu hỏi bình thường — cứ hỏi, mình sẵn lòng giúp.",
    "lock_busy": "Yêu cầu trước đó vẫn đang xử lý. Đợi hoặc thử lại sau.",
    "pick_lock_busy": "Đợi chút, yêu cầu trước đó vẫn đang xử lý.",
    "stream_note_send_fail": "\n\n[không gửi được phần tiếp theo của tin nhắn]",
    "stream_note_interrupted": "\n\n[mất kết nối — câu trả lời có thể chưa đầy đủ]",
    "inline_answer_title": "Câu trả lời của bot",
    "reset_deny": "Trong nhóm, chỉ quản trị viên, người tạo nhóm hoặc chủ bot mới xóa được lịch sử. Trong tin nhắn riêng thì ai cũng dùng được.",
    "reset_done": "Lịch sử trò chuyện trong chat này đã xóa. Bắt đầu lại từ đầu.",
    "logs_deny": "Không có quyền dùng lệnh này.",
    "logs_group_only": "Lệnh này hiện log kỹ thuật — chỉ dùng được trong tin nhắn riêng với bot, không trong nhóm.",
    "logs_empty": "File log trống hoặc chưa được tạo.",
    "logs_send_error": "Lỗi khi gửi log: {error}",
    "stats_deny": "Không có quyền dùng lệnh này.",
    "stats_group_only": "Lệnh này hiện số liệu kỹ thuật — chỉ dùng được trong tin nhắn riêng với bot, không trong nhóm.",
    "stats_no_data": "  không có dữ liệu",
    "stats_limit_used": " (hết hạn mức)",
    "tiktok_sound_recognized": "Đã nhận ra liên kết âm thanh TikTok.",
    "tiktok_sound_no_separate": "Không tải riêng âm thanh qua liên kết trang của nó được — cả TikWM lẫn TikTok đều không cấp cho bot dữ liệu cần thiết với loại liên kết đó. Hãy gửi liên kết tới video bất kỳ có âm thanh này — bot sẽ gửi âm thanh kèm theo.",
    "tiktok_dl_hd": "Đang tải video không watermark (HD)",
    "tiktok_dl_as_is": "Không thấy bản không watermark — tải nguyên như vậy",
    "tiktok_dl_plain": "Đang tải video không watermark",
    "tiktok_too_big": "Video TikTok này quá lớn để gửi — Bot API Telegram giới hạn tải file lên 50 MB. Thử tải video này bằng cách khác.",
    "tiktok_no_media": "Đã nhận ra liên kết, nhưng TikTok không đưa video lẫn ảnh — nội dung có thể đã xóa hoặc không khả dụng.",
    "tiktok_processing": "Đang xử lý liên kết TikTok",
    "tiktok_fetch_fail": "Không lấy được video từ liên kết này — có thể riêng tư, đã xóa, chặn vùng hoặc link hỏng.",
    "tiktok_generic_fail": "Không tải được video hay slideshow này từ TikTok. Thử liên kết khác hoặc lặp lại sau một lúc.",
    "tiktok_author": "Tác giả TikTok",
    "tiktok_music": "Nhạc từ TikTok",
    "status_generating_image": "Đang tạo ảnh",
    "status_voicing": "Đang đọc văn bản",
    "status_taking_longer": "Việc này sẽ mất lâu hơn một chút…",
    "status_listening": "Đang nghe.",
    "pick_suffix": "\n\n(hoặc cứ viết bằng chữ)",
    "pick_expired": "Các nút đã hết hạn — viết bằng chữ.",
    "pick_not_yours": "Đó không phải nút của bạn.",
    "pick_choice": "Lựa chọn: {choice}",
    "owner_quota_notice": "⚠️ Hạn mức Gemini đã hết sạch trên mọi model của tuyến (xem /stats để biết chi tiết).",
}

PACK_TH: dict[str, str] = {
    "lang_title": "เลือกภาษาของบอท:",
    "lang_done": "ภาษา: ไทย",
    "lang_deny": "ในกลุ่ม เฉพาะแอดมิน ผู้สร้างกลุ่ม หรือเจ้าของบอทเท่านั้นที่เปลี่ยนภาษาได้ ส่วนในแชทส่วนตัวใครก็ใช้ได้",
    "cmd_desc_start": "เกี่ยวกับบอทและรายการคำสั่ง",
    "cmd_desc_reset": "ล้างประวัติการสนทนา",
    "cmd_desc_draw": "วาดรูปจากคำอธิบาย",
    "cmd_desc_tts": "อ่านข้อความเป็นเสียง",
    "cmd_desc_lang": "ภาษาของบอท",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "ตอบคำถาม (ค้นเว็บเมื่อจำเป็น) อ่านเว็บและวิดีโอ YouTube ผ่านลิงก์ วิเคราะห์รูปภาพ วิดีโอ เสียง และเอกสาร วาดรูปจากคำอธิบาย และอ่านข้อความเป็นเสียง\n\n"
        "<b>คำสั่ง</b>\n"
        "/draw [คำอธิบาย] — วาดรูป\n"
        "/tts [ข้อความ] — อ่านข้อความเป็นเสียง\n"
        "/reset — ล้างประวัติการสนทนา\n"
        "/lang — ภาษาของบอท\n\n"
        "วาดรูปและอ่านออกเสียงด้วยคำพูดก็ได้ ไม่ต้องใช้คำสั่ง — เช่น “วาดรูปแมว” หรือ “อ่านอันนี้ออกเสียง”\n\n"
        "<b>TikTok</b>\n"
        "ส่งลิงก์มา — จะดาวน์โหลดวิดีโอหรือรูปภาพแบบไม่มีลายน้ำ\n\n"
        "ถามอะไรมาก็ได้ — กำลังฟัง"
    ),
    "draw_empty": "เพิ่มข้อความหลังคำสั่ง /draw ตัวอย่าง: /draw สถานีอวกาศ",
    "tts_empty": "เพิ่มข้อความหลังคำสั่ง /tts ตัวอย่าง: /tts สวัสดีตอนเช้า",
    "tts_too_long": "ข้อความยาวเกินไปสำหรับการอ่านออกเสียง (จำกัด {limit} ตัวอักษร ตอนนี้ {length}) ย่อข้อความแล้วลองใหม่",
    "draw_err_unavailable": "บริการสร้างรูปภาพไม่พร้อมใช้งานชั่วคราว ลองใหม่ภายหลัง",
    "draw_err_overloaded": "บริการสร้างรูปภาพกำลังโหลดหนัก รอหนึ่งนาทีแล้วลองใหม่",
    "draw_err_gone": "บริการสร้างรูปภาพไม่พร้อมใช้งานในขณะนี้ ลองใหม่ภายหลัง",
    "draw_err_budget": "การสร้างรูปภาพใช้เวลานานเกินไปในขณะนี้ กรุณาลองใหม่อีกครั้งในหนึ่งนาที",
    "draw_err_generic": "สร้างรูปภาพไม่สำเร็จ ลองใหม่หรืออธิบายใหม่",
    "tts_err_exhausted": "โควต้าคำขอเสียงหมดชั่วคราว ลองใหม่ในอีกสักครู่",
    "tts_err_generic": "อ่านข้อความออกเสียงไม่ได้ ลองใหม่หรือย่อข้อความ",
    "rate_limited": "คุณส่งคำขอมากเกินไป รอสักครู่",
    "model_err_rate_limit": "โควต้าคำขอหมดแล้วในขณะนี้ รอสักครู่แล้วลองใหม่",
    "model_err_paid": "บริการไม่พร้อมใช้งานชั่วคราว ลองส่งคำขอใหม่อีกสักครู่",
    "model_err_forbidden": "เกิดข้อผิดพลาดการเข้าถึงบริการชั่วคราว ลองใหม่",
    "model_err_unavailable": "บริการไม่พร้อมใช้งานชั่วคราว ลองส่งคำขอใหม่อีกสักครู่",
    "model_err_fallback": "เกิดข้อผิดพลาดของบริการชั่วคราว ลองใหม่อีกสักครู่",
    "err_quota_exhausted": "โควต้าคำขอฟรีหมดแล้ว — นี่คือขีดจำกัดรายวันจริงของบริการ ไม่ใช่ข้อผิดพลาด ลองใหม่ภายหลัง",
    "err_youtube_fail": "เปิดวิดีโอนี้ไม่ได้ (อาจเป็นส่วนตัว ถูกลบ ยาวเกินไป หรือวิเคราะห์ไม่ได้) ช่วยอธิบายเป็นคำพูดว่าเป็นเรื่องอะไร แล้วจะช่วยได้",
    "err_budget": "บริการกำลังโหลดหนัก กรุณาลองใหม่อีกครั้งในหนึ่งนาที",
    "injection_probe_reply": "ไม่เปิดเผยหรือหารือการตั้งค่าและคำสั่งในรูปแบบนั้น ถ้ามีคำถามทั่วไป — ถามได้เลย ยินดีช่วย",
    "lock_busy": "คำขอก่อนหน้ายังประมวลผลอยู่ รอหรือลองใหม่ภายหลัง",
    "pick_lock_busy": "รอก่อน คำขอก่อนหน้ายังประมวลผลอยู่",
    "stream_note_send_fail": "\n\n[ส่งส่วนที่เหลือของข้อความไม่ได้]",
    "stream_note_interrupted": "\n\n[การเชื่อมต่อขาด — คำตอบอาจไม่สมบูรณ์]",
    "inline_answer_title": "คำตอบของบอท",
    "reset_deny": "ในกลุ่ม เฉพาะแอดมิน ผู้สร้างกลุ่ม หรือเจ้าของบอทเท่านั้นที่ล้างประวัติได้ ส่วนในแชทส่วนตัวใครก็ใช้ได้",
    "reset_done": "ล้างประวัติการสนทนาในแชทนี้แล้ว เริ่มกันใหม่",
    "logs_deny": "ไม่มีสิทธิ์ใช้คำสั่งนี้",
    "logs_group_only": "คำสั่งนี้แสดงล็อกทางเทคนิค — ใช้ได้เฉพาะในแชทส่วนตัวกับบอท ไม่ใช่ในกลุ่ม",
    "logs_empty": "ไฟล์ล็อกว่างเปล่าหรือยังไม่ถูกสร้าง",
    "logs_send_error": "เกิดข้อผิดพลาดขณะส่งล็อก: {error}",
    "stats_deny": "ไม่มีสิทธิ์ใช้คำสั่งนี้",
    "stats_group_only": "คำสั่งนี้แสดงสถิติทางเทคนิค — ใช้ได้เฉพาะในแชทส่วนตัวกับบอท ไม่ใช่ในกลุ่ม",
    "stats_no_data": "  ไม่มีข้อมูล",
    "stats_limit_used": " (โควต้าหมด)",
    "tiktok_sound_recognized": "รู้จักลิงก์เสียง TikTok แล้ว",
    "tiktok_sound_no_separate": "ดาวน์โหลดเสียงแยกผ่านลิงก์หน้าของมันไม่ได้ — ทั้ง TikWM และ TikTok ไม่ให้ข้อมูลที่จำเป็นกับบอทนี้สำหรับลิงก์แบบนั้น ส่งลิงก์วิดีโอใดก็ได้ที่มีเสียงนี้มา — บอทจะส่งเสียงมาด้วย",
    "tiktok_dl_hd": "กำลังดาวน์โหลดวิดีโอแบบไม่มีลายน้ำ (HD)",
    "tiktok_dl_as_is": "ไม่พบเวอร์ชันไม่มีลายน้ำ — ดาวน์โหลดตามสภาพ",
    "tiktok_dl_plain": "กำลังดาวน์โหลดวิดีโอแบบไม่มีลายน้ำ",
    "tiktok_too_big": "วิดีโอ TikTok นี้ใหญ่เกินกว่าจะส่ง — Bot API ของ Telegram จำกัดอัปโหลดไฟล์ที่ 50 MB ลองดาวน์โหลดวิดีโอนี้ด้วยวิธีอื่น",
    "tiktok_no_media": "รู้จักลิงก์แล้ว แต่ TikTok ไม่ให้ทั้งวิดีโอและรูปภาพ — เนื้อหาอาจถูกลบหรือใช้ไม่ได้",
    "tiktok_processing": "กำลังประมวลผลลิงก์ TikTok",
    "tiktok_fetch_fail": "ดึงวิดีโอจากลิงก์นี้ไม่ได้ — อาจเป็นส่วนตัว ถูกลบ ถูกบล็อกตามภูมิภาค หรือลิงก์เสีย",
    "tiktok_generic_fail": "ดาวน์โหลดวิดีโอหรือสไลด์โชว์นี้จาก TikTok ไม่ได้ ลองลิงก์อื่นหรือทำซ้ำอีกสักครู่",
    "tiktok_author": "ผู้เขียน TikTok",
    "tiktok_music": "เพลงจาก TikTok",
    "status_generating_image": "กำลังสร้างรูปภาพ",
    "status_voicing": "กำลังอ่านข้อความ",
    "status_taking_longer": "อันนี้จะใช้เวลานานกว่านิดหน่อย…",
    "status_listening": "กำลังฟัง",
    "pick_suffix": "\n\n(หรือพิมพ์เป็นข้อความก็ได้)",
    "pick_expired": "ปุ่มหมดอายุแล้ว — พิมพ์เป็นข้อความ",
    "pick_not_yours": "นั่นไม่ใช่ปุ่มของคุณ",
    "pick_choice": "ตัวเลือก: {choice}",
    "owner_quota_notice": "⚠️ โควต้า Gemini หมดเกลี้ยงในทุกโมเดลของเส้นทาง (ดูรายละเอียดที่ /stats)",
}

PACK_FA: dict[str, str] = {
    "lang_title": "زبان ربات را انتخاب کنید:",
    "lang_done": "زبان: فارسی.",
    "lang_deny": "در گروه‌ها فقط مدیر، سازنده گروه یا مالک ربات می‌تواند زبان را تغییر دهد. در پیام‌های خصوصی برای همه در دسترس است.",
    "cmd_desc_start": "درباره ربات و فهرست دستورها",
    "cmd_desc_reset": "پاک کردن سابقه گفتگو",
    "cmd_desc_draw": "کشیدن تصویر از روی توضیح",
    "cmd_desc_tts": "تبدیل متن به گفتار",
    "cmd_desc_lang": "زبان ربات",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "من به سؤال‌ها جواب می‌دهم (با جستجوی اینترنتی در صورت نیاز)، سایت‌ها و ویدیوهای YouTube را از روی لینک می‌خوانم، عکس‌ها، ویدیوها، صداها و سندها را تحلیل می‌کنم، از روی توضیح تصویر می‌کشم و متن را به گفتار تبدیل می‌کنم.\n\n"
        "<b>دستورها</b>\n"
        "/draw [توضیح] — کشیدن تصویر\n"
        "/tts [متن] — تبدیل متن به گفتار\n"
        "/reset — پاک کردن سابقه گفتگو\n"
        "/lang — زبان ربات\n\n"
        "می‌توانی بدون دستور هم فقط با کلمات بخواهی بکشم یا بخوانم — مثلاً «یک گربه بکش» یا «این را بلند بخوان».\n\n"
        "<b>TikTok</b>\n"
        "لینک بفرست — ویدیو یا عکس‌ها را بدون واترمارک دانلود می‌کنم.\n\n"
        "هر چه می‌خواهی بپرس — گوش می‌دهم."
    ),
    "draw_empty": "بعد از دستور /draw متن بنویس. مثال: /draw ایستگاه فضایی",
    "tts_empty": "بعد از دستور /tts متن بنویس. مثال: /tts صبح بخیر",
    "tts_too_long": "متن برای گفتار خیلی طولانی است (سقف {limit} نویسه، الان {length}). متن را کوتاه کن و دوباره تلاش کن.",
    "draw_err_unavailable": "سرویس تولید تصویر موقتاً در دسترس نیست. بعداً تلاش کن.",
    "draw_err_overloaded": "سرویس تولید تصویر الان پرترافیک است. یک دقیقه صبر کن و دوباره تلاش کن.",
    "draw_err_gone": "سرویس تولید تصویر الان در دسترس نیست. بعداً تلاش کن.",
    "draw_err_budget": "تولید تصویر الان خیلی طول می‌کشد. لطفاً یک دقیقه دیگر دوباره تلاش کن.",
    "draw_err_generic": "تولید تصویر ناموفق بود. دوباره تلاش کن یا توضیح را بازنویسی کن.",
    "tts_err_exhausted": "سقف درخواست‌های گفتار موقتاً تمام شده است. کمی بعد تلاش کن.",
    "tts_err_generic": "نتوانستم متن را به گفتار تبدیل کنم. دوباره تلاش کن یا متن را کوتاه کن.",
    "rate_limited": "درخواست‌های زیادی می‌فرستی. کمی صبر کن.",
    "model_err_rate_limit": "سقف درخواست‌ها الان تمام شده است. کمی صبر کن و دوباره تلاش کن.",
    "model_err_paid": "سرویس موقتاً در دسترس نیست. کمی بعد درخواست را تکرار کن.",
    "model_err_forbidden": "خطای موقت دسترسی به سرویس. دوباره تلاش کن.",
    "model_err_unavailable": "سرویس موقتاً در دسترس نیست. کمی بعد درخواست را تکرار کن.",
    "model_err_fallback": "خطای موقت سرویس. کمی بعد تلاش کن.",
    "err_quota_exhausted": "سقف درخواست‌های رایگان تمام شده است — این سقف روزانه واقعی سرویس است، نه خطا. بعداً تلاش کن.",
    "err_youtube_fail": "نتوانستم این ویدیو را باز کنم (شاید خصوصی، حذف‌شده، خیلی طولانی یا برای تحلیل در دسترس نیست). لطفاً با کلمات بگو درباره چیست — آن‌وقت می‌توانم کمک کنم.",
    "err_budget": "سرویس الان پرترافیک است. لطفاً یک دقیقه دیگر دوباره تلاش کن.",
    "injection_probe_reply": "پیکربندی و دستورهایم را در این قالب فاش نمی‌کنم و بحث نمی‌کنم. اگر سؤال عادی داری — بپرس، با کمال میل کمک می‌کنم.",
    "lock_busy": "درخواست قبلی هنوز در حال پردازش است. صبر کن یا بعداً تلاش کن.",
    "pick_lock_busy": "صبر کن، درخواست قبلی هنوز در حال پردازش است.",
    "stream_note_send_fail": "\n\n[ادامه پیام ارسال نشد]",
    "stream_note_interrupted": "\n\n[اتصال قطع شد — ممکن است پاسخ ناقص باشد]",
    "inline_answer_title": "پاسخ ربات",
    "reset_deny": "در گروه فقط مدیر، سازنده گروه یا مالک ربات سابقه را پاک می‌کند. در پیام‌های خصوصی برای همه در دسترس است.",
    "reset_done": "سابقه گفتگو در این چت پاک شد. از نو شروع می‌کنیم.",
    "logs_deny": "به این دستور دسترسی نداری.",
    "logs_group_only": "این دستور لاگ‌های فنی را نشان می‌دهد — فقط در پیام‌های خصوصی با ربات در دسترس است، نه در گروه‌ها.",
    "logs_empty": "فایل لاگ خالی است یا هنوز ساخته نشده است.",
    "logs_send_error": "خطا در ارسال لاگ‌ها: {error}",
    "stats_deny": "به این دستور دسترسی نداری.",
    "stats_group_only": "این دستور آمار فنی را نشان می‌دهد — فقط در پیام‌های خصوصی با ربات در دسترس است، نه در گروه‌ها.",
    "stats_no_data": "  داده‌ای نیست",
    "stats_limit_used": " (سقف تمام شد)",
    "tiktok_sound_recognized": "لینک صدای TikTok شناخته شد.",
    "tiktok_sound_no_separate": "نمی‌شود صدا را جدا از طریق لینک صفحه‌اش دانلود کرد — نه TikWM نه خود TikTok داده لازم برای این نوع لینک را به این ربات نمی‌دهند. لطفاً لینک هر ویدیویی با این صدا را بفرست — ربات صدا را همراهش می‌فرستد.",
    "tiktok_dl_hd": "در حال دانلود ویدیوی بدون واترمارک (HD)",
    "tiktok_dl_as_is": "نسخه بدون واترمارک پیدا نشد — همان‌طور که هست دانلود می‌شود",
    "tiktok_dl_plain": "در حال دانلود ویدیوی بدون واترمارک",
    "tiktok_too_big": "این ویدیوی TikTok برای ارسال خیلی بزرگ است — Bot API تلگرام آپلود فایل را به ۵۰ مگابایت محدود می‌کند. ویدیو را به روش دیگری دانلود کن.",
    "tiktok_no_media": "لینک شناخته شد، اما TikTok نه ویدیو داد نه عکس — شاید محتوا حذف شده یا در دسترس نیست.",
    "tiktok_processing": "در حال پردازش لینک TikTok",
    "tiktok_fetch_fail": "نتوانستم ویدیوی این لینک را بگیرم — شاید خصوصی، حذف‌شده، محدودشده منطقه‌ای است یا لینک خراب است.",
    "tiktok_generic_fail": "نتوانستم این ویدیو یا اسلایدشو را از TikTok دانلود کنم. لینک دیگری امتحان کن یا کمی بعد تکرار کن.",
    "tiktok_author": "نویسنده TikTok",
    "tiktok_music": "موسیقی از TikTok",
    "status_generating_image": "در حال تولید تصویر",
    "status_voicing": "در حال تبدیل متن به گفتار",
    "status_taking_longer": "این کمی بیشتر طول می‌کشد…",
    "status_listening": "گوش می‌دهم.",
    "pick_suffix": "\n\n(یا فقط متنی بنویس)",
    "pick_expired": "دکمه‌ها منقضی شدند — متنی بنویس.",
    "pick_not_yours": "این‌ها دکمه‌های تو نیستند.",
    "pick_choice": "انتخاب: {choice}",
    "owner_quota_notice": "⚠️ سهمیه Gemini در همه مدل‌های مسیر کاملاً تمام شد (جزئیات: /stats).",
}

PACK_UR: dict[str, str] = {
    "lang_title": "بوٹ کی زبان منتخب کریں:",
    "lang_done": "زبان: اردو۔",
    "lang_deny": "گروپ میں زبان صرف ایڈمن، گروپ بنانے والا یا بوٹ کا مالک بدل سکتا ہے۔ ڈائریکٹ پیغامات میں یہ سب کے لیے دستیاب ہے۔",
    "cmd_desc_start": "بوٹ کے بارے میں اور کمانڈز کی فہرست",
    "cmd_desc_reset": "مکالمے کی تاریخ صاف کریں",
    "cmd_desc_draw": "تفصیل سے تصویر بنائیں",
    "cmd_desc_tts": "متن کو آواز دیں",
    "cmd_desc_lang": "بوٹ کی زبان",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "میں سوالوں کے جواب دیتا ہوں (ضرورت پر انٹرنیٹ تلاش کے ساتھ)، لنک سے ویب سائٹس اور YouTube ویڈیوز پڑھتا ہوں، تصاویر، ویڈیوز، آڈیو اور دستاویزات کا تجزیہ کرتا ہوں، تفصیل سے تصویر بناتا ہوں اور متن کو آواز دیتا ہوں۔\n\n"
        "<b>کمانڈز</b>\n"
        "/draw [تفصیل] — تصویر بنائیں\n"
        "/tts [متن] — متن کو آواز دیں\n"
        "/reset — مکالمے کی تاریخ صاف کریں\n"
        "/lang — بوٹ کی زبان\n\n"
        "بغیر کمانڈ، صرف لفظوں سے بھی بنوا اور سن سکتے ہو — مثلاً “بلی بناؤ” یا “یہ زور سے پڑھو”۔\n\n"
        "<b>TikTok</b>\n"
        "لنک بھیجو — ویڈیو یا تصاویر بغیر واٹرمارک ڈاؤن لوڈ کر دوں گا۔\n\n"
        "کچھ بھی پوچھو — سن رہا ہوں۔"
    ),
    "draw_empty": "/draw کمانڈ کے بعد متن لکھو۔ مثال: /draw خلائی اسٹیشن",
    "tts_empty": "/tts کمانڈ کے بعد متن لکھو۔ مثال: /tts صبح بخیر",
    "tts_too_long": "متن آواز کے لیے بہت لمبا ہے (حد {limit} حروف، ابھی {length})۔ متن چھوٹا کر کے پھر کوشش کرو۔",
    "draw_err_unavailable": "تصویر بنانے کی سروس عارضی طور پر دستیاب نہیں ہے۔ بعد میں کوشش کرو۔",
    "draw_err_overloaded": "تصویر بنانے کی سروس ابھی بہت مصروف ہے۔ ایک منٹ رکو اور پھر کوشش کرو۔",
    "draw_err_gone": "تصویر بنانے کی سروس ابھی دستیاب نہیں ہے۔ بعد میں کوشش کرو۔",
    "draw_err_budget": "تصویر بنانے میں ابھی بہت وقت لگ رہا ہے۔ براہ کرم ایک منٹ بعد پھر کوشش کرو۔",
    "draw_err_generic": "تصویر بنانے میں خرابی ہوئی۔ پھر کوشش کرو یا تفصیل بدل کر لکھو۔",
    "tts_err_exhausted": "آواز کی درخواستوں کی حد عارضی طور پر ختم ہو گئی ہے۔ تھوڑی دیر بعد کوشش کرو۔",
    "tts_err_generic": "متن کو آواز نہ دے سکے۔ پھر کوشش کرو یا متن چھوٹا کرو۔",
    "rate_limited": "تم بہت سی درخواستیں بھیج رہے ہو۔ تھوڑا رکو۔",
    "model_err_rate_limit": "درخواستوں کی حد ابھی ختم ہو گئی ہے۔ تھوڑا رک کر پھر کوشش کرو۔",
    "model_err_paid": "سروس عارضی طور پر دستیاب نہیں ہے۔ تھوڑی دیر بعد درخواست دہراؤ۔",
    "model_err_forbidden": "سروس تک رسائی میں عارضی خرابی۔ پھر کوشش کرو۔",
    "model_err_unavailable": "سروس عارضی طور پر دستیاب نہیں ہے۔ تھوڑی دیر بعد درخواست دہراؤ۔",
    "model_err_fallback": "سروس میں عارضی خرابی۔ تھوڑی دیر بعد کوشش کرو۔",
    "err_quota_exhausted": "مفت درخواستوں کی حد ختم ہو گئی ہے — یہ سروس کی اصل روزانہ حد ہے، کوئی خرابی نہیں۔ بعد میں کوشش کرو۔",
    "err_youtube_fail": "یہ ویڈیو کھولی نہ جا سکی (شاید نجی ہے، ہٹائی گئی ہے، بہت لمبی ہے یا تجزیے کے لیے دستیاب نہیں ہے)۔ براہ کرم لفظوں میں بتاؤ یہ کس بارے میں ہے — تب مدد کر سکوں گا۔",
    "err_budget": "سروس ابھی بہت مصروف ہے۔ براہ کرم ایک منٹ بعد پھر کوشش کرو۔",
    "injection_probe_reply": "میں اپنی سیٹنگ اور ہدایات کو اس شکل میں نہ بتاتا ہوں، نہ بحث کرتا ہوں۔ اگر کوئی عام سوال ہے — پوچھو، خوشی سے مدد کروں گا۔",
    "lock_busy": "پچھلی درخواست ابھی زیرِ عمل ہے۔ رکو یا بعد میں کوشش کرو۔",
    "pick_lock_busy": "رکو، پچھلی درخواست ابھی زیرِ عمل ہے۔",
    "stream_note_send_fail": "\n\n[پیغام کا باقی حصہ بھیجا نہ جا سکا]",
    "stream_note_interrupted": "\n\n[کنکشن ٹوٹ گیا — جواب ادھورا ہو سکتا ہے]",
    "inline_answer_title": "بوٹ کا جواب",
    "reset_deny": "گروپ میں تاریخ صرف ایڈمن، گروپ بنانے والا یا بوٹ کا مالک صاف کرتا ہے۔ ڈائریکٹ پیغامات میں یہ سب کے لیے دستیاب ہے۔",
    "reset_done": "اس چیٹ میں مکالمے کی تاریخ صاف ہو گئی۔ نئے سرے سے شروع کرتے ہیں۔",
    "logs_deny": "اس کمانڈ تک رسائی نہیں ہے۔",
    "logs_group_only": "یہ کمانڈ تکنیکی لاگز دکھاتی ہے — صرف بوٹ کے ساتھ ڈائریکٹ پیغامات میں دستیاب ہے، گروپ میں نہیں۔",
    "logs_empty": "لاگ فائل خالی ہے یا ابھی بنائی نہیں گئی ہے۔",
    "logs_send_error": "لاگز بھیجنے میں خرابی: {error}",
    "stats_deny": "اس کمانڈ تک رسائی نہیں ہے۔",
    "stats_group_only": "یہ کمانڈ تکنیکی اعداد دکھاتی ہے — صرف بوٹ کے ساتھ ڈائریکٹ پیغامات میں دستیاب ہے، گروپ میں نہیں۔",
    "stats_no_data": "  کوئی ڈیٹا نہیں",
    "stats_limit_used": " (حد ختم)",
    "tiktok_sound_recognized": "TikTok آواز کا لنک پہچانا گیا۔",
    "tiktok_sound_no_separate": "آواز کو اس کے پیج کے لنک سے الگ ڈاؤن لوڈ نہیں کیا جا سکتا — نہ TikWM نہ خود TikTok اس طرح کے لنک کے لیے اس بوٹ کو ضروری ڈیٹا دیتے ہیں۔ براہ کرم اس آواز والی کسی بھی ویڈیو کا لنک بھیجیں — بوٹ آواز اس کے ساتھ بھیجے گا۔",
    "tiktok_dl_hd": "بغیر واٹرمارک ویڈیو ڈاؤن لوڈ ہو رہی ہے (HD)",
    "tiktok_dl_as_is": "بغیر واٹرمارک والا نسخہ نہیں ملا — جیسی ہے ویسی ڈاؤن لوڈ ہو رہی ہے",
    "tiktok_dl_plain": "بغیر واٹرمارک ویڈیو ڈاؤن لوڈ ہو رہی ہے",
    "tiktok_too_big": "یہ TikTok ویڈیو بھیجنے کے لیے بہت بڑی ہے — Telegram Bot API فائل اپلوڈ کو 50 MB تک محدود کرتا ہے۔ اس ویڈیو کو کسی اور طریقے سے ڈاؤن لوڈ کرنے کی کوشش کرو۔",
    "tiktok_no_media": "لنک پہچانا گیا، لیکن TikTok نے نہ ویڈیو دی نہ تصاویر — شاید مواد ہٹایا گیا ہے یا دستیاب نہیں ہے۔",
    "tiktok_processing": "TikTok لنک زیرِ عمل ہے",
    "tiktok_fetch_fail": "اس لنک سے ویڈیو لائی نہ جا سکی — شاید نجی ہے، ہٹائی گئی ہے، علاقائی پابندی ہے یا لنک ٹوٹا ہے۔",
    "tiktok_generic_fail": "یہ ویڈیو یا سلائیڈشو TikTok سے ڈاؤن لوڈ نہ ہو سکا۔ دوسرا لنک آزماؤ یا تھوڑی دیر بعد دہراؤ۔",
    "tiktok_author": "TikTok مصنف",
    "tiktok_music": "TikTok موسیقی",
    "status_generating_image": "تصویر بنائی جا رہی ہے",
    "status_voicing": "متن کو آواز دی جا رہی ہے",
    "status_taking_longer": "اس میں تھوڑا اور وقت لگے گا…",
    "status_listening": "سن رہا ہوں۔",
    "pick_suffix": "\n\n(یا بس متن میں لکھو)",
    "pick_expired": "بٹن ختم ہو گئے — متن میں لکھو۔",
    "pick_not_yours": "یہ تمہارے بٹن نہیں ہیں۔",
    "pick_choice": "انتخاب: {choice}",
    "owner_quota_notice": "⚠️ روٹ کے تمام ماڈلز میں Gemini کوٹا مکمل ختم (تفصیل کے لیے /stats)۔",
}

PACK_UZ: dict[str, str] = {
    "lang_title": "Bot tilini tanlang:",
    "lang_done": "Til: O'zbekcha.",
    "lang_deny": "Guruhda tilni faqat admin, guruh yaratuvchisi yoki bot egasi o'zgartira oladi. Shaxsiy xabarlarda hamma uchun ochiq.",
    "cmd_desc_start": "Bot haqida va buyruqlar ro'yxati",
    "cmd_desc_reset": "Muloqot tarixini tozalash",
    "cmd_desc_draw": "Tavsifdan rasm chizish",
    "cmd_desc_tts": "Matnni ovozlash",
    "cmd_desc_lang": "Bot tili",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Savollarga javob beraman (kerak bo'lsa internetdan qidirib), havola orqali saytlar va YouTube videolarni o'qiyman, foto, video, audio va hujjatlarni tahlil qilaman, tavsifdan rasm chizaman va matnni ovozlayman.\n\n"
        "<b>Buyruqlar</b>\n"
        "/draw [tavsif] — rasm chizish\n"
        "/tts [matn] — matnni ovozlash\n"
        "/reset — muloqot tarixini tozalash\n"
        "/lang — bot tili\n\n"
        "Buyruqsiz, oddiy so'z bilan ham chizdirib ovozlatish mumkin — masalan “mushuk chiz” yoki “shuni ovozla”.\n\n"
        "<b>TikTok</b>\n"
        "Havola yubor — videoni yoki fotolarni suv belgisiz yuklab beraman.\n\n"
        "Xohlaganingni so'ra — eshityapman."
    ),
    "draw_empty": "/draw buyrug'idan keyin matn yoz. Masalan: /draw kosmik stansiya",
    "tts_empty": "/tts buyrug'idan keyin matn yoz. Masalan: /tts Xayrli tong",
    "tts_too_long": "Matn ovozlash uchun juda uzun (limit {limit} belgi, hozir {length}). Matnni qisqartirib qayta urin.",
    "draw_err_unavailable": "Rasm yaratish xizmati vaqtincha mavjud emas. Keyinroq urin.",
    "draw_err_overloaded": "Rasm yaratish xizmati hozir juda band. Bir daqiqa kutib qayta urin.",
    "draw_err_gone": "Rasm yaratish xizmati hozir mavjud emas. Keyinroq urin.",
    "draw_err_budget": "Rasm yaratish hozir juda uzoq cho'zilmoqda. Iltimos, bir daqiqadan keyin qayta urin.",
    "draw_err_generic": "Rasm yaratishda xatolik yuz berdi. Qayta urin yoki tavsifni boshqacha yoz.",
    "tts_err_exhausted": "Ovozlash so'rovlari limiti vaqtincha tugadi. Birozdan keyin urin.",
    "tts_err_generic": "Matnni ovozlab bo'lmadi. Qayta urin yoki matnni qisqartir.",
    "rate_limited": "Juda ko'p so'rov yuboryapsan. Biroz kut.",
    "model_err_rate_limit": "So'rovlar limiti hozir tugadi. Biroz kutib qayta urin.",
    "model_err_paid": "Xizmat vaqtincha mavjud emas. So'rovni birozdan keyin takrorla.",
    "model_err_forbidden": "Xizmatga kirishda vaqtinchalik xato. Qayta urin.",
    "model_err_unavailable": "Xizmat vaqtincha mavjud emas. So'rovni birozdan keyin takrorla.",
    "model_err_fallback": "Xizmatda vaqtinchalik xato. Birozdan keyin urin.",
    "err_quota_exhausted": "Bepul so'rovlar limiti tugadi — bu xato emas, xizmatning haqiqiy kunlik limiti. Keyinroq urin.",
    "err_youtube_fail": "Bu videoni ochib bo'lmadi (shaxsiy, o'chirilgan, juda uzun yoki tahlil qilib bo'lmaydigan bo'lishi mumkin). Nima haqida ekanini so'z bilan ayt — shunda yordam bera olaman.",
    "err_budget": "Xizmat hozir juda band. Iltimos, bir daqiqadan keyin qayta urin.",
    "injection_probe_reply": "Sozlamalarim va ko'rsatmalarimni bu formatda oshkor qilmayman, muhokama ham qilmayman. Oddiy savoling bo'lsa — so'ra, jon deb yordam beraman.",
    "lock_busy": "Oldingi so'rov hali bajarilmoqda. Kut yoki keyinroq urin.",
    "pick_lock_busy": "Kut, oldingi so'rov hali bajarilmoqda.",
    "stream_note_send_fail": "\n\n[xabar davomi yuborilmadi]",
    "stream_note_interrupted": "\n\n[aloqa uzildi — javob to'liq bo'lmasligi mumkin]",
    "inline_answer_title": "Bot javobi",
    "reset_deny": "Guruhda tarixni faqat admin, guruh yaratuvchisi yoki bot egasi tozalaydi. Shaxsiy xabarlarda hamma uchun ochiq.",
    "reset_done": "Bu chatda muloqot tarixi tozalandi. Yangidan boshlaymiz.",
    "logs_deny": "Bu buyruqqa kirish yo'q.",
    "logs_group_only": "Bu buyruq texnik loglarni ko'rsatadi — faqat bot bilan shaxsiy xabarlarda ochiq, guruhlarda emas.",
    "logs_empty": "Log fayli bo'sh yoki hali yaratilmagan.",
    "logs_send_error": "Loglarni yuborishda xato: {error}",
    "stats_deny": "Bu buyruqqa kirish yo'q.",
    "stats_group_only": "Bu buyruq texnik statistikani ko'rsatadi — faqat bot bilan shaxsiy xabarlarda ochiq, guruhlarda emas.",
    "stats_no_data": "  ma'lumot yo'q",
    "stats_limit_used": " (limit tugadi)",
    "tiktok_sound_recognized": "TikTok tovush havolasi tanildi.",
    "tiktok_sound_no_separate": "Tovushni sahifa havolasi orqali alohida yuklab bo'lmaydi — bu havola turi uchun bu botga kerakli ma'lumotni na TikWM na TikTokning o'zi beradi. Iltimos, shu tovushli istalgan videoning havolasini yubor — bot tovushni u bilan birga yuboradi.",
    "tiktok_dl_hd": "Suv belgisiz video yuklanmoqda (HD)",
    "tiktok_dl_as_is": "Suv belgisiz nusxa topilmadi — boricha yuklanmoqda",
    "tiktok_dl_plain": "Suv belgisiz video yuklanmoqda",
    "tiktok_too_big": "Bu TikTok videosi yuborish uchun juda katta — Telegram Bot API fayl yuklashni 50 MB bilan cheklaydi. Videoni boshqa yo'l bilan yuklashga urin.",
    "tiktok_no_media": "Havola tanildi, lekin TikTok na video berdi na foto — kontent o'chirilgan yoki mavjud bo'lmasligi mumkin.",
    "tiktok_processing": "TikTok havolasi qayta ishlanmoqda",
    "tiktok_fetch_fail": "Bu havoladan videoni olib bo'lmadi — shaxsiy, o'chirilgan, hudud cheklovli bo'lishi yoki havola buzilgan bo'lishi mumkin.",
    "tiktok_generic_fail": "Bu video yoki slaydshouni TikTok'dan yuklab bo'lmadi. Boshqa havola urin yoki birozdan keyin takrorla.",
    "tiktok_author": "TikTok muallifi",
    "tiktok_music": "TikTok musiqasi",
    "status_generating_image": "Rasm yaratilmoqda",
    "status_voicing": "Matn ovozlanmoqda",
    "status_taking_longer": "Bu biroz ko'proq vaqt oladi…",
    "status_listening": "Eshityapman.",
    "pick_suffix": "\n\n(yoki shunchaki matn bilan yoz)",
    "pick_expired": "Tugmalar eskirgan — matn bilan yoz.",
    "pick_not_yours": "Bular sening tugmalaring emas.",
    "pick_choice": "Tanlov: {choice}",
    "owner_quota_notice": "⚠️ Marshrutdagi barcha modellarda Gemini kvotasi butunlay tugadi (batafsil /stats).",
}

PACK_BN: dict[str, str] = {
    "lang_title": "বটের ভাষা বেছে নাও:",
    "lang_done": "ভাষা: বাংলা।",
    "lang_deny": "গ্রুপে ভাষা কেবল অ্যাডমিন, গ্রুপ নির্মাতা বা বটের মালিক বদলাতে পারে। ডাইরেক্ট মেসেজে সবার জন্য উন্মুক্ত।",
    "cmd_desc_start": "বট সম্পর্কে ও কমান্ড তালিকা",
    "cmd_desc_reset": "সংলাপ ইতিহাস মুছুন",
    "cmd_desc_draw": "বিবরণ থেকে ছবি আঁকুন",
    "cmd_desc_tts": "টেক্সটে কণ্ঠ দিন",
    "cmd_desc_lang": "বটের ভাষা",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "আমি প্রশ্নের উত্তর দিই (দরকারে ইন্টারনেট খুঁজে), লিংক থেকে ওয়েবসাইট ও YouTube ভিডিও পড়ি, ছবি, ভিডিও, অডিও ও নথি বিশ্লেষণ করি, বিবরণ থেকে ছবি আঁকি ও টেক্সটে কণ্ঠ দিই।\n\n"
        "<b>কমান্ড</b>\n"
        "/draw [বিবরণ] — ছবি আঁকুন\n"
        "/tts [টেক্সট] — টেক্সটে কণ্ঠ দিন\n"
        "/reset — সংলাপ ইতিহাস মুছুন\n"
        "/lang — বটের ভাষা\n\n"
        "কমান্ড ছাড়া শুধু কথায়ও আঁকাতে ও শোনাতে পারো — যেমন “বিড়াল আঁকো” বা “এটা জোরে পড়ো”।\n\n"
        "<b>TikTok</b>\n"
        "লিংক পাঠাও — ভিডিও বা ছবি ওয়াটারমার্ক ছাড়া ডাউনলোড করে দেব।\n\n"
        "যা খুশি জিজ্ঞেস করো — শুনছি।"
    ),
    "draw_empty": "/draw কমান্ডের পর টেক্সট লেখো। উদাহরণ: /draw মহাকাশ স্টেশন",
    "tts_empty": "/tts কমান্ডের পর টেক্সট লেখো। উদাহরণ: /tts সুপ্রভাত",
    "tts_too_long": "টেক্সট কণ্ঠের জন্য অনেক লম্বা (সীমা {limit} অক্ষর, এখন {length})। টেক্সট ছোট করে আবার চেষ্টা করো।",
    "draw_err_unavailable": "ছবি তৈরির পরিষেবা সাময়িকভাবে অনুপলব্ধ। পরে চেষ্টা করো।",
    "draw_err_overloaded": "ছবি তৈরির পরিষেবা এখন অতিভারাক্রান্ত। এক মিনিট অপেক্ষা করে আবার চেষ্টা করো।",
    "draw_err_gone": "ছবি তৈরির পরিষেবা এখন অনুপলব্ধ। পরে চেষ্টা করো।",
    "draw_err_budget": "ছবি তৈরি এখন অনেক সময় নিচ্ছে। দয়া করে এক মিনিট পর আবার চেষ্টা করো।",
    "draw_err_generic": "ছবি তৈরিতে ত্রুটি হয়েছে। আবার চেষ্টা করো বা বিবরণ বদলে লেখো।",
    "tts_err_exhausted": "কণ্ঠ অনুরোধের সীমা সাময়িকভাবে শেষ। একটু পরে চেষ্টা করো।",
    "tts_err_generic": "টেক্সটে কণ্ঠ দেওয়া যায়নি। আবার চেষ্টা করো বা টেক্সট ছোট করো।",
    "rate_limited": "তুমি অনেক বেশি অনুরোধ পাঠাচ্ছ। একটু অপেক্ষা করো।",
    "model_err_rate_limit": "অনুরোধের সীমা এখন শেষ। একটু অপেক্ষা করে আবার চেষ্টা করো।",
    "model_err_paid": "পরিষেবা সাময়িকভাবে অনুপলব্ধ। একটু পরে অনুরোধ পুনরাবৃত্তি করো।",
    "model_err_forbidden": "পরিষেবায় প্রবেশে সাময়িক ত্রুটি। আবার চেষ্টা করো।",
    "model_err_unavailable": "পরিষেবা সাময়িকভাবে অনুপলব্ধ। একটু পরে অনুরোধ পুনরাবৃত্তি করো।",
    "model_err_fallback": "পরিষেবায় সাময়িক ত্রুটি। একটু পরে চেষ্টা করো।",
    "err_quota_exhausted": "বিনামূল্যে অনুরোধের সীমা শেষ — এটি পরিষেবার আসল দৈনিক সীমা, কোনো ত্রুটি নয়। পরে চেষ্টা করো।",
    "err_youtube_fail": "এই ভিডিও খোলা যায়নি (হয়তো ব্যক্তিগত, মোছা, অনেক লম্বা বা বিশ্লেষণের অনুপযুক্ত)। কথায় বর্ণনা করো এটি কী নিয়ে — তাহলে সাহায্য করতে পারব।",
    "err_budget": "পরিষেবা এখন অতিভারাক্রান্ত। দয়া করে এক মিনিট পর আবার চেষ্টা করো।",
    "injection_probe_reply": "আমি আমার সেটিং ও নির্দেশ এই ফরম্যাটে প্রকাশ বা আলোচনা করি না। সাধারণ প্রশ্ন থাকলে — জিজ্ঞেস করো, খুশি হয়ে সাহায্য করব।",
    "lock_busy": "আগের অনুরোধ এখনও প্রক্রিয়াধীন। অপেক্ষা করো বা পরে চেষ্টা করো।",
    "pick_lock_busy": "অপেক্ষা করো, আগের অনুরোধ এখনও প্রক্রিয়াধীন।",
    "stream_note_send_fail": "\n\n[বার্তার বাকি অংশ পাঠানো যায়নি]",
    "stream_note_interrupted": "\n\n[সংযোগ বিচ্ছিন্ন — উত্তর অসম্পূর্ণ হতে পারে]",
    "inline_answer_title": "বটের উত্তর",
    "reset_deny": "গ্রুপে ইতিহাস কেবল অ্যাডমিন, গ্রুপ নির্মাতা বা বটের মালিক মোছে। ডাইরেক্ট মেসেজে সবার জন্য উন্মুক্ত।",
    "reset_done": "এই চ্যাটে সংলাপ ইতিহাস মোছা হয়েছে। নতুন করে শুরু করি।",
    "logs_deny": "এই কমান্ডে প্রবেশাধিকার নেই।",
    "logs_group_only": "এই কমান্ড প্রযুক্তিগত লগ দেখায় — কেবল বটের সাথে ডাইরেক্ট মেসেজে উপলব্ধ, গ্রুপে নয়।",
    "logs_empty": "লগ ফাইল খালি বা এখনও তৈরি হয়নি।",
    "logs_send_error": "লগ পাঠাতে ত্রুটি: {error}",
    "stats_deny": "এই কমান্ডে প্রবেশাধিকার নেই।",
    "stats_group_only": "এই কমান্ড প্রযুক্তিগত পরিসংখ্যান দেখায় — কেবল বটের সাথে ডাইরেক্ট মেসেজে উপলব্ধ, গ্রুপে নয়।",
    "stats_no_data": "  কোনো তথ্য নেই",
    "stats_limit_used": " (সীমা শেষ)",
    "tiktok_sound_recognized": "TikTok সাউন্ড লিংক চেনা গেছে।",
    "tiktok_sound_no_separate": "পেজের লিংক দিয়ে সাউন্ড আলাদা ডাউনলোড করা যাবে না — TikWM বা TikTok কেউই এই ধরনের লিংকের জন্য এই বটকে প্রয়োজনীয় তথ্য দেয় না। এই সাউন্ডের যেকোনো ভিডিওর লিংক পাঠাও — বট সাউন্ডসহ পাঠাবে।",
    "tiktok_dl_hd": "ওয়াটারমার্ক ছাড়া ভিডিও ডাউনলোড হচ্ছে (HD)",
    "tiktok_dl_as_is": "ওয়াটারমার্ক ছাড়া সংস্করণ পাওয়া যায়নি — যেমন আছে তেমন ডাউনলোড হচ্ছে",
    "tiktok_dl_plain": "ওয়াটারমার্ক ছাড়া ভিডিও ডাউনলোড হচ্ছে",
    "tiktok_too_big": "এই TikTok ভিডিও পাঠানোর জন্য অনেক বড় — Telegram Bot API ফাইল আপলোড 50 MB-এ সীমাবদ্ধ। ভিডিওটি অন্য উপায়ে ডাউনলোড করার চেষ্টা করো।",
    "tiktok_no_media": "লিংক চেনা গেছে, কিন্তু TikTok ভিডিও বা ছবি কিছুই দেয়নি — হয়তো কন্টেন্ট মোছা বা অনুপলব্ধ।",
    "tiktok_processing": "TikTok লিংক প্রক্রিয়াধীন",
    "tiktok_fetch_fail": "এই লিংক থেকে ভিডিও আনা যায়নি — হয়তো ব্যক্তিগত, মোছা, অঞ্চল-নিষিদ্ধ বা লিংক নষ্ট।",
    "tiktok_generic_fail": "TikTok থেকে এই ভিডিও বা স্লাইডশো ডাউনলোড করা যায়নি। অন্য লিংক চেষ্টা করো বা একটু পরে পুনরাবৃত্তি করো।",
    "tiktok_author": "TikTok লেখক",
    "tiktok_music": "TikTok সঙ্গীত",
    "status_generating_image": "ছবি তৈরি হচ্ছে",
    "status_voicing": "টেক্সটে কণ্ঠ দেওয়া হচ্ছে",
    "status_taking_longer": "এতে আরও একটু সময় লাগবে…",
    "status_listening": "শুনছি।",
    "pick_suffix": "\n\n(বা শুধু টেক্সটে লেখো)",
    "pick_expired": "বোতাম মেয়াদোত্তীর্ণ — টেক্সটে লেখো।",
    "pick_not_yours": "এগুলো তোমার বোতাম নয়।",
    "pick_choice": "পছন্দ: {choice}",
    "owner_quota_notice": "⚠️ রুটের সব মডেলে Gemini কোটা সম্পূর্ণ শেষ (বিস্তারিত /stats)।",
}

PACK_FIL: dict[str, str] = {
    "lang_title": "Piliin ang wika ng bot:",
    "lang_done": "Wika: Filipino.",
    "lang_deny": "Sa grupo, admin, tagalikha ng grupo, o may-ari ng bot lang ang puwedeng magpalit ng wika. Sa direct message, available sa lahat.",
    "cmd_desc_start": "Tungkol sa bot at listahan ng command",
    "cmd_desc_reset": "Burahin ang history ng usapan",
    "cmd_desc_draw": "Gumuhit ng larawan mula sa paglalarawan",
    "cmd_desc_tts": "Basahin ang text",
    "cmd_desc_lang": "Wika ng bot",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Sumasagot ako sa mga tanong (may web search kung kailangan), nagbabasa ng mga site at YouTube video sa link, nagsusuri ng mga photo, video, audio at dokumento, gumuguhit ng mga larawan mula sa paglalarawan, at nagbabasa ng text.\n\n"
        "<b>Mga command</b>\n"
        "/draw [paglalarawan] — gumuhit ng larawan\n"
        "/tts [text] — basahin ang text\n"
        "/reset — burahin ang history ng usapan\n"
        "/lang — wika ng bot\n\n"
        "Puwede ring gumuhit at magpabasa sa salita lang, walang command — halimbawa “gumuhit ng pusa” o “basahin ito nang malakas”.\n\n"
        "<b>TikTok</b>\n"
        "Mag-send ng link — ida-download ko ang video o mga photo nang walang watermark.\n\n"
        "Magtanong ka lang — nakikinig ako."
    ),
    "draw_empty": "Magdagdag ng text pagkatapos ng /draw. Halimbawa: /draw istasyon ng kalawakan",
    "tts_empty": "Magdagdag ng text pagkatapos ng /tts. Halimbawa: /tts Magandang umaga",
    "tts_too_long": "Masyadong mahaba ang text para basahin (limit {limit} character, ngayon {length}). Paikliin ang text at subukan uli.",
    "draw_err_unavailable": "Hindi pansamantalang available ang serbisyo sa pagguhit. Subukan uli mamaya.",
    "draw_err_overloaded": "Overloaded ang serbisyo sa pagguhit ngayon. Maghintay ng isang minuto at subukan uli.",
    "draw_err_gone": "Hindi available ang serbisyo sa pagguhit ngayon. Subukan uli mamaya.",
    "draw_err_budget": "Masyadong matagal ang pagguhit ngayon. Pakisubukan uli pagkaraan ng isang minuto.",
    "draw_err_generic": "Nabigo ang pagguhit. Subukan uli o baguhin ang paglalarawan.",
    "tts_err_exhausted": "Naubos pansamantala ang limit sa voice request. Subukan uli mamaya.",
    "tts_err_generic": "Hindi mabasa ang text. Subukan uli o paikliin ang text.",
    "rate_limited": "Masyado kang maraming request na sinesend. Maghintay ka muna.",
    "model_err_rate_limit": "Naubos ang limit sa request ngayon. Maghintay ka muna at subukan uli.",
    "model_err_paid": "Hindi pansamantalang available ang serbisyo. Ulitin ang request mamaya.",
    "model_err_forbidden": "Pansamantalang error sa access ng serbisyo. Subukan uli.",
    "model_err_unavailable": "Hindi pansamantalang available ang serbisyo. Ulitin ang request mamaya.",
    "model_err_fallback": "Pansamantalang error ng serbisyo. Subukan uli mamaya.",
    "err_quota_exhausted": "Naubos ang libreng limit sa request — iyan ang tunay na daily limit ng serbisyo, hindi error. Subukan uli mamaya.",
    "err_youtube_fail": "Hindi mabuksan ang video na ito (baka private, binura, masyadong mahaba, o hindi available para i-analyze). Ilarawan sa salita kung tungkol saan — tutulungan kita.",
    "err_budget": "Overloaded ang serbisyo ngayon. Pakisubukan uli pagkaraan ng isang minuto.",
    "injection_probe_reply": "Hindi ko nire-reveal o dinediscuss ang configuration at instruction ko sa format na iyan. Kung may ordinaryong tanong — itanong mo, tutulong ako nang masaya.",
    "lock_busy": "Pinoproseso pa ang nakaraang request. Maghintay o subukan uli mamaya.",
    "pick_lock_busy": "Teka, pinoproseso pa ang nakaraang request.",
    "stream_note_send_fail": "\n\n[hindi na-send ang karugtong ng mensahe]",
    "stream_note_interrupted": "\n\n[naputol ang koneksyon — baka hindi kumpleto ang sagot]",
    "inline_answer_title": "Sagot ng bot",
    "reset_deny": "Sa grupo, admin, tagalikha ng grupo, o may-ari ng bot lang ang nagbubura ng history. Sa direct message, available sa lahat.",
    "reset_done": "Nabura ang history ng usapan sa chat na ito. Umpisa uli.",
    "logs_deny": "Walang access sa command na ito.",
    "logs_group_only": "Pinapakita ng command na ito ang mga technical log — sa direct message lang sa bot available, hindi sa grupo.",
    "logs_empty": "Walang laman ang log file o hindi pa nagawa.",
    "logs_send_error": "Error sa pag-send ng mga log: {error}",
    "stats_deny": "Walang access sa command na ito.",
    "stats_group_only": "Pinapakita ng command na ito ang mga technical stat — sa direct message lang sa bot available, hindi sa grupo.",
    "stats_no_data": "  walang data",
    "stats_limit_used": " (ubos ang limit)",
    "tiktok_sound_recognized": "Nakilala ang TikTok sound link.",
    "tiktok_sound_no_separate": "Hindi mada-download nang hiwalay ang sound sa link ng page nito — hindi binibigyan ng TikWM o TikTok mismo ang bot na ito ng data para sa ganoong link. Mag-send ng link sa kahit anong video na may sound na ito — isesend ng bot ang sound kasama nito.",
    "tiktok_dl_hd": "Dina-download ang video nang walang watermark (HD)",
    "tiktok_dl_as_is": "Walang nahanap na bersyong walang watermark — dina-download kung ano",
    "tiktok_dl_plain": "Dina-download ang video nang walang watermark",
    "tiktok_too_big": "Masyadong malaki ang TikTok video na ito para i-send — nililimitahan ng Telegram Bot API ang upload ng file sa 50 MB. Subukang i-download ang video sa ibang paraan.",
    "tiktok_no_media": "Nakilala ang link, pero walang binigay ang TikTok na video o photo — baka binura o hindi available ang content.",
    "tiktok_processing": "Pinoproseso ang TikTok link",
    "tiktok_fetch_fail": "Hindi makuha ang video mula sa link na ito — baka private, binura, region-blocked, o sira ang link.",
    "tiktok_generic_fail": "Hindi mada-download ang video o slideshow na ito mula sa TikTok. Subukan ang ibang link o ulitin mamaya.",
    "tiktok_author": "May-akda ng TikTok",
    "tiktok_music": "Musika mula sa TikTok",
    "status_generating_image": "Gumuguhit ng larawan",
    "status_voicing": "Binabasa ang text",
    "status_taking_longer": "Medyo matatagalan ito…",
    "status_listening": "Nakikinig.",
    "pick_suffix": "\n\n(o isulat lang sa text)",
    "pick_expired": "Expired na ang mga button — isulat sa text.",
    "pick_not_yours": "Hindi sa iyo ang mga button na iyan.",
    "pick_choice": "Pinili: {choice}",
    "owner_quota_notice": "⚠️ Ubos na ubos ang Gemini quota sa lahat ng model ng ruta (tingnan ang /stats para sa detalye).",
}

PACK_PL: dict[str, str] = {
    "lang_title": "Wybierz język bota:",
    "lang_done": "Język: polski.",
    "lang_deny": "W grupie język zmienia tylko admin, twórca grupy lub właściciel bota. W wiadomościach prywatnych — dostępne dla wszystkich.",
    "cmd_desc_start": "O bocie i lista komend",
    "cmd_desc_reset": "Wyczyść historię dialogu",
    "cmd_desc_draw": "Narysuj obraz z opisu",
    "cmd_desc_tts": "Odczytaj tekst",
    "cmd_desc_lang": "Język bota",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Odpowiadam na pytania (w razie potrzeby z wyszukiwaniem w internecie), czytam strony i filmy YouTube z linku, analizuję zdjęcia, filmy, audio i dokumenty, rysuję obrazy z opisu i odczytuję tekst.\n\n"
        "<b>Komendy</b>\n"
        "/draw [opis] — narysuj obraz\n"
        "/tts [tekst] — odczytaj tekst\n"
        "/reset — wyczyść historię dialogu\n"
        "/lang — język bota\n\n"
        "Rysować i odczytywać można też zwykłymi słowami, bez komend — np. „narysuj kota” albo „przeczytaj to na głos”.\n\n"
        "<b>TikTok</b>\n"
        "Wyślij link — pobiorę film lub zdjęcia bez znaków wodnych.\n\n"
        "Pytaj o co chcesz — słucham."
    ),
    "draw_empty": "Dopisz tekst po komendzie /draw. Przykład: /draw stacja kosmiczna",
    "tts_empty": "Dopisz tekst po komendzie /tts. Przykład: /tts Dzień dobry",
    "tts_too_long": "Tekst jest za długi do odczytania (limit {limit} znaków, teraz {length}). Skróć tekst i spróbuj ponownie.",
    "draw_err_unavailable": "Usługa generowania obrazów jest chwilowo niedostępna. Spróbuj później.",
    "draw_err_overloaded": "Usługa generowania obrazów jest teraz przeciążona. Poczekaj minutę i spróbuj ponownie.",
    "draw_err_gone": "Usługa generowania obrazów jest teraz niedostępna. Spróbuj później.",
    "draw_err_budget": "Generowanie obrazu trwa teraz zbyt długo. Spróbuj ponownie za minutę.",
    "draw_err_generic": "Błąd generowania obrazu. Spróbuj ponownie lub przeformułuj opis.",
    "tts_err_exhausted": "Limit próśb o odczytanie jest chwilowo wyczerpany. Spróbuj nieco później.",
    "tts_err_generic": "Nie udało się odczytać tekstu. Spróbuj ponownie lub skróć tekst.",
    "rate_limited": "Wysyłasz za dużo próśb. Poczekaj chwilę.",
    "model_err_rate_limit": "Limit próśb jest teraz wyczerpany. Poczekaj chwilę i spróbuj ponownie.",
    "model_err_paid": "Usługa jest chwilowo niedostępna. Powtórz prośbę nieco później.",
    "model_err_forbidden": "Chwilowy błąd dostępu do usługi. Spróbuj ponownie.",
    "model_err_unavailable": "Usługa jest chwilowo niedostępna. Powtórz prośbę nieco później.",
    "model_err_fallback": "Chwilowy błąd usługi. Spróbuj nieco później.",
    "err_quota_exhausted": "Darmowy limit próśb wyczerpany — to prawdziwy dzienny limit usługi, nie błąd. Spróbuj później.",
    "err_youtube_fail": "Nie udało się otworzyć tego filmu (może prywatny, usunięty, za długi albo niedostępny do analizy). Opisz słowami, o czym jest — wtedy pomogę.",
    "err_budget": "Usługa jest teraz przeciążona. Spróbuj ponownie za minutę.",
    "injection_probe_reply": "Swojej konfiguracji i instrukcji w takim formacie nie ujawniam ani nie omawiam. Jeśli masz zwykłe pytanie — pytaj, chętnie pomogę.",
    "lock_busy": "Poprzednia prośba jest jeszcze przetwarzana. Poczekaj lub spróbuj później.",
    "pick_lock_busy": "Poczekaj, poprzednia prośba jest jeszcze przetwarzana.",
    "stream_note_send_fail": "\n\n[nie udało się wysłać dalszej części wiadomości]",
    "stream_note_interrupted": "\n\n[połączenie przerwane — odpowiedź może być niepełna]",
    "inline_answer_title": "Odpowiedź bota",
    "reset_deny": "W grupie historię czyści tylko admin, twórca grupy lub właściciel bota. W wiadomościach prywatnych — dostępne dla wszystkich.",
    "reset_done": "Historia dialogu na tym czacie wyczyszczona. Zaczynamy od czystej kartki.",
    "logs_deny": "Brak dostępu do tej komendy.",
    "logs_group_only": "Ta komenda pokazuje logi techniczne — dostępna tylko w wiadomościach prywatnych z botem, nie w grupach.",
    "logs_empty": "Plik logów jest pusty albo jeszcze nie powstał.",
    "logs_send_error": "Błąd wysyłania logów: {error}",
    "stats_deny": "Brak dostępu do tej komendy.",
    "stats_group_only": "Ta komenda pokazuje statystyki techniczne — dostępna tylko w wiadomościach prywatnych z botem, nie w grupach.",
    "stats_no_data": "  brak danych",
    "stats_limit_used": " (limit wyczerpany)",
    "tiktok_sound_recognized": "Rozpoznano link do dźwięku TikToka.",
    "tiktok_sound_no_separate": "Dźwięku nie da się pobrać osobno linkiem do jego strony — ani TikWM, ani sam TikTok nie dają temu botowi potrzebnych danych dla takiego rodzaju linku. Wyślij proszę link do dowolnego filmu z tym dźwiękiem — bot przyśle dźwięk razem z nim.",
    "tiktok_dl_hd": "Pobieram film bez znaków wodnych (HD)",
    "tiktok_dl_as_is": "Nie znalazłem wersji bez znaków wodnych — pobieram jak jest",
    "tiktok_dl_plain": "Pobieram film bez znaków wodnych",
    "tiktok_too_big": "Ten film z TikToka jest za duży do wysłania — Bot API Telegram ogranicza wysyłanie plików do 50 MB. Spróbuj pobrać ten film inaczej.",
    "tiktok_no_media": "Link rozpoznany, ale TikTok nie oddał ani filmu, ani zdjęć — treść może usunięta albo niedostępna.",
    "tiktok_processing": "Przetwarzam link TikToka",
    "tiktok_fetch_fail": "Nie udało się pobrać filmu z tego linku — może prywatny, usunięty, zablokowany regionalnie albo link uszkodzony.",
    "tiktok_generic_fail": "Nie udało się pobrać tego filmu ani pokazu z TikToka. Spróbuj inny link albo powtórz nieco później.",
    "tiktok_author": "Autor TikToka",
    "tiktok_music": "Muzyka z TikToka",
    "status_generating_image": "Generuję obraz",
    "status_voicing": "Odczytuję tekst",
    "status_taking_longer": "To zajmie trochę więcej czasu…",
    "status_listening": "Słucham.",
    "pick_suffix": "\n\n(albo po prostu napisz tekstem)",
    "pick_expired": "Przyciski wygasły — napisz tekstem.",
    "pick_not_yours": "To nie twoje przyciski.",
    "pick_choice": "Wybór: {choice}",
    "owner_quota_notice": "⚠️ Limit Gemini wyczerpany w całości we wszystkich modelach trasy (szczegóły w /stats).",
}

PACK_NL: dict[str, str] = {
    "lang_title": "Kies de taal van de bot:",
    "lang_done": "Taal: Nederlands.",
    "lang_deny": "In groepen kunnen alleen een admin, de groepsmaker of de bot-eigenaar de taal wijzigen. In directe berichten is ze voor iedereen beschikbaar.",
    "cmd_desc_start": "Over de bot en commandolijst",
    "cmd_desc_reset": "Dialooggeschiedenis wissen",
    "cmd_desc_draw": "Afbeelding tekenen uit beschrijving",
    "cmd_desc_tts": "Tekst uitspreken",
    "cmd_desc_lang": "Taal van de bot",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Ik beantwoord vragen (zo nodig met webzoekopdracht), lees websites en YouTube-video's via link, analyseer foto's, video's, audio en documenten, teken afbeeldingen uit een beschrijving en spreek tekst uit.\n\n"
        "<b>Commando's</b>\n"
        "/draw [beschrijving] — afbeelding tekenen\n"
        "/tts [tekst] — tekst uitspreken\n"
        "/reset — dialooggeschiedenis wissen\n"
        "/lang — taal van de bot\n\n"
        "Tekenen en uitspreken kan ook gewoon met woorden, zonder commando's — bijv. “teken een kat” of “lees dit hardop voor”.\n\n"
        "<b>TikTok</b>\n"
        "Stuur een link — ik download de video of foto's zonder watermerk.\n\n"
        "Vraag maar — ik luister."
    ),
    "draw_empty": "Voeg tekst toe na het commando /draw. Voorbeeld: /draw ruimtestation",
    "tts_empty": "Voeg tekst toe na het commando /tts. Voorbeeld: /tts Goedemorgen",
    "tts_too_long": "Tekst is te lang om uit te spreken (limiet {limit} tekens, nu {length}). Kort de tekst in en probeer opnieuw.",
    "draw_err_unavailable": "Afbeeldingsservice is tijdelijk niet beschikbaar. Probeer later.",
    "draw_err_overloaded": "Afbeeldingsservice is nu overbelast. Wacht een minuut en probeer opnieuw.",
    "draw_err_gone": "Afbeeldingsservice is nu niet beschikbaar. Probeer later.",
    "draw_err_budget": "Afbeelding maken duurt nu te lang. Probeer het over een minuut nog eens.",
    "draw_err_generic": "Afbeelding maken mislukt. Probeer opnieuw of herformuleer de beschrijving.",
    "tts_err_exhausted": "Spraaklimiet is tijdelijk op. Probeer het later nog eens.",
    "tts_err_generic": "Tekst kon niet worden uitgesproken. Probeer opnieuw of kort de tekst in.",
    "rate_limited": "Je stuurt te veel verzoeken. Wacht even.",
    "model_err_rate_limit": "Verzoeklimiet is nu op. Wacht even en probeer opnieuw.",
    "model_err_paid": "Service is tijdelijk niet beschikbaar. Herhaal het verzoek later nog eens.",
    "model_err_forbidden": "Tijdelijke toegangsfout. Probeer opnieuw.",
    "model_err_unavailable": "Service is tijdelijk niet beschikbaar. Herhaal het verzoek later nog eens.",
    "model_err_fallback": "Tijdelijke servicefout. Probeer het later nog eens.",
    "err_quota_exhausted": "Gratis verzoeklimiet is op — dat is de echte daglimiet van de service, geen fout. Probeer later.",
    "err_youtube_fail": "Deze video kon niet worden geopend (misschien privé, verwijderd, te lang of niet analyseerbaar). Beschrijf in woorden waar hij over gaat — dan kan ik helpen.",
    "err_budget": "Service is nu overbelast. Probeer het over een minuut nog eens.",
    "injection_probe_reply": "Mijn configuratie en instructies bespreek ik in dat formaat niet. Bij een gewone vraag — stel hem gerust, ik help graag.",
    "lock_busy": "Vorig verzoek wordt nog verwerkt. Wacht of probeer later.",
    "pick_lock_busy": "Wacht, vorig verzoek wordt nog verwerkt.",
    "stream_note_send_fail": "\n\n[rest van het bericht kon niet worden verzonden]",
    "stream_note_interrupted": "\n\n[verbinding verbroken — antwoord is misschien onvolledig]",
    "inline_answer_title": "Antwoord van de bot",
    "reset_deny": "In groepen wist alleen een admin, de groepsmaker of de bot-eigenaar de geschiedenis. In directe berichten is het voor iedereen beschikbaar.",
    "reset_done": "Dialooggeschiedenis in deze chat gewist. Schone lei.",
    "logs_deny": "Geen toegang tot dit commando.",
    "logs_group_only": "Dit commando toont technische logs — alleen beschikbaar in directe berichten met de bot, niet in groepen.",
    "logs_empty": "Logbestand is leeg of nog niet gemaakt.",
    "logs_send_error": "Fout bij verzenden van logs: {error}",
    "stats_deny": "Geen toegang tot dit commando.",
    "stats_group_only": "Dit commando toont technische statistieken — alleen beschikbaar in directe berichten met de bot, niet in groepen.",
    "stats_no_data": "  geen data",
    "stats_limit_used": " (limiet op)",
    "tiktok_sound_recognized": "TikTok-geluidslink herkend.",
    "tiktok_sound_no_separate": "Geluid kan niet apart worden gedownload via de paginalink — TikWM noch TikTok zelf geven deze bot de benodigde data voor dat linktype. Stuur een link naar een willekeurige video met dit geluid — de bot stuurt het geluid dan mee.",
    "tiktok_dl_hd": "Video zonder watermerk downloaden (HD)",
    "tiktok_dl_as_is": "Geen versie zonder watermerk gevonden — downloaden zoals het is",
    "tiktok_dl_plain": "Video zonder watermerk downloaden",
    "tiktok_too_big": "Deze TikTok-video is te groot om te verzenden — de Telegram Bot API limiteert bestandsuploads tot 50 MB. Probeer deze video anders te downloaden.",
    "tiktok_no_media": "Link herkend, maar TikTok gaf video noch foto's — inhoud misschien verwijderd of niet beschikbaar.",
    "tiktok_processing": "TikTok-link verwerken",
    "tiktok_fetch_fail": "Video van deze link kon niet worden opgehaald — misschien privé, verwijderd, regiogeblokkeerd of link stuk.",
    "tiktok_generic_fail": "Deze video of slideshow kon niet van TikTok worden gedownload. Probeer een andere link of herhaal later.",
    "tiktok_author": "TikTok-auteur",
    "tiktok_music": "Muziek van TikTok",
    "status_generating_image": "Afbeelding genereren",
    "status_voicing": "Tekst uitspreken",
    "status_taking_longer": "Dit duurt iets langer…",
    "status_listening": "Ik luister.",
    "pick_suffix": "\n\n(of schrijf het gewoon als tekst)",
    "pick_expired": "Knoppen verlopen — schrijf als tekst.",
    "pick_not_yours": "Dit zijn niet jouw knoppen.",
    "pick_choice": "Keuze: {choice}",
    "owner_quota_notice": "⚠️ Gemini-quota volledig op over alle routemodellen (details zie /stats).",
}

PACK_ZU: dict[str, str] = {
    "lang_title": "Khetha ulimi lwe-bot:",
    "lang_done": "Ulimi: isiZulu.",
    "lang_deny": "Eqenjini, i-admin, umdali weqenjiniso, noma umnikazi we-bot kuphela angashintsha ulimi. Emiyalezo eqondile, kutholakala kuwo wonke umuntu.",
    "cmd_desc_start": "Mayelana ne-bot nohlu lwemiyalo",
    "cmd_desc_reset": "Sula umlando wengxoxo",
    "cmd_desc_draw": "Dweba isithombe ngencazelo",
    "cmd_desc_tts": "Funda umbiko",
    "cmd_desc_lang": "Ulimi lwe-bot",
    "start_text": (
        "<b>Lumen</b>\n\n"
        "Ngiphendula imibuzo (ngosesho lwewebhu uma kudingeka), ngifunda ama-site namavidiyo e-YouTube ngesixhumanisi, ngihlaziya izithombe, amavidiyo, umsindo nemibhalo, ngidweba izithombe ngencazelo, futhi ngifunda umbiko.\n\n"
        "<b>Imiyalo</b>\n"
        "/draw [incazelo] — dweba isithombe\n"
        "/tts [umbiko] — funda umbiko\n"
        "/reset — sula umlando wengxoxo\n"
        "/lang — ulimi lwe-bot\n\n"
        "Ungadweba futhi ufundise ngamazwi kuphela, ngaphandle kwemiyalo — isibonelo “dweba ikati” noma “funda lokhu kuzwakale”.\n\n"
        "<b>TikTok</b>\n"
        "Thumela isixhumanisi — ngizolanda ividiyo noma izithombe ngaphandle kwe-watermark.\n\n"
        "Buza noma yini — ngilalele."
    ),
    "draw_empty": "Nezela umbiko ngemva komyalo /draw. Isibonelo: /draw isiteshi sasemkhathini",
    "tts_empty": "Nezela umbiko ngemva komyalo /tts. Isibonelo: /tts Sawubona ekuseni",
    "tts_too_long": "Umbiko mude kakhulu ukufundwa (umkhawulo {limit} izinhlamvu, manje {length}). Finyeza umbiko bese uzama futhi.",
    "draw_err_unavailable": "Isevisi yokudweba izithombe ayitholakali okwamanje. Zama futhi kamuva.",
    "draw_err_overloaded": "Isevisi yokudweba izithombe igcwele kakhulu manje. Linda umzuzu bese uzama futhi.",
    "draw_err_gone": "Isevisi yokudweba izithombe ayitholakali manje. Zama futhi kamuva.",
    "draw_err_budget": "Ukudweba isithombe kuthatha isikhathi eside kakhulu manje. Ngicela uzame futhi ngomzuzu.",
    "draw_err_generic": "Ukudweba isithombe kwehlulekile. Zama futhi noma ubhale incazelo ngenye indlela.",
    "tts_err_exhausted": "Umkhawulo wezicelo zezwi uphelile okwamanje. Zama futhi maduzane.",
    "tts_err_generic": "Umbiko awukwazanga ukufundwa. Zama futhi noma wufinyeze.",
    "rate_limited": "Uthumela izicelo eziningi kakhulu. Linda kancane.",
    "model_err_rate_limit": "Umkhawulo wezicelo uphelile manje. Linda kancane bese uzama futhi.",
    "model_err_paid": "Isevisi ayitholakali okwamanje. Phinda isicelo maduzane.",
    "model_err_forbidden": "Iphutha lesikhashana lokufinyelela isevisi. Zama futhi.",
    "model_err_unavailable": "Isevisi ayitholakali okwamanje. Phinda isicelo maduzane.",
    "model_err_fallback": "Iphutha lesikhashana lesevisi. Zama futhi maduzane.",
    "err_quota_exhausted": "Umkhawulo wamahhala wezicelo uphelile — lowo umkhawulo wangempela wansuku zonke wesevisi, hhayi iphutha. Zama futhi kamuva.",
    "err_youtube_fail": "Le vidiyo ayikwazanga ukuvulwa (mhlawumbe eyimfihlo, isusiwe, inde kakhulu, noma ayikwazi ukuhlaziywa). Chaza ngamazwi ukuthi imayelana nani — ngizokusiza.",
    "err_budget": "Isevisi igcwele kakhulu manje. Ngicela uzame futhi ngomzuzu.",
    "injection_probe_reply": "Angikudaluli noma ngixoxe ngokucushwa nemiyalelo yami ngaleyo fomethi. Uma unombuzo ojwayelekile — buza, ngizosiza ngenjabulo.",
    "lock_busy": "Isicelo sangaphambilini sisacutshungulwa. Linda noma uzame futhi kamuva.",
    "pick_lock_busy": "Linda, isicelo sangaphambilini sisacutshungulwa.",
    "stream_note_send_fail": "\n\n[ingxenye esele yomlayezo ayithunyelwanga]",
    "stream_note_interrupted": "\n\n[uxhumo lunqamukile — impendulo ingaphelele]",
    "inline_answer_title": "Impendulo ye-bot",
    "reset_deny": "Eqenjini, i-admin, umdali weqenjiniso, noma umnikazi we-bot kuphela osula umlando. Emiyalezo eqondile, kutholakala kuwo wonke umuntu.",
    "reset_done": "Umlando wengxoxo kule chat usuliwe. Siqala kabusha.",
    "logs_deny": "Alikho ilungelo lalo myalo.",
    "logs_group_only": "Lo myalo ubonisa ama-log ezobuchwepheshe — utholakala kuphela emiyalezo eqondile ne-bot, hhayi eqenjini.",
    "logs_empty": "Ifayela ye-log alinalutho noma alikadalwa.",
    "logs_send_error": "Iphutha ekuthumeleni ama-log: {error}",
    "stats_deny": "Alikho ilungelo lalo myalo.",
    "stats_group_only": "Lo myalo ubonisa izibalo zobuchwepheshe — utholakala kuphela emiyalezo eqondile ne-bot, hhayi eqenjini.",
    "stats_no_data": "  ayikho idatha",
    "stats_limit_used": " (umkhawulo uphelile)",
    "tiktok_sound_recognized": "Isixhumanisi somsindo se-TikTok saziwe.",
    "tiktok_sound_no_separate": "Umsindo awukwazi ukulandwa wedwa ngesixhumanisi sekhasi lawo — i-TikWM noma i-TikTok uqobo aluniki le bot idatha edingekayo kwalolo hlobo lwesixhumanisi. Thumela isixhumanisi sanoma iyiphi ividiyo enalo msindo — i-bot izothumela umsindo nawo.",
    "tiktok_dl_hd": "Kulanda ividiyo ngaphandle kwe-watermark (HD)",
    "tiktok_dl_as_is": "Ayitholakalanga inguqulo engena-watermark — kulanda njengoba injalo",
    "tiktok_dl_plain": "Kulanda ividiyo ngaphandle kwe-watermark",
    "tiktok_too_big": "Le vidiyo ye-TikTok inkulu kakhulu ukuthunyelwa — i-Telegram Bot API ikhawulela ukulayishwa kwefayela ku-50 MB. Zama ukulanda le vidiyo ngenye indlela.",
    "tiktok_no_media": "Isixhumanisi saziwe, kodwa i-TikTok ayinikanga vidiyo noma zithombe — okuqukethwe kungasuswa noma kungatholakali.",
    "tiktok_processing": "Kucutshungulwa isixhumanisi se-TikTok",
    "tiktok_fetch_fail": "Ayikwazanga ukuthathwa ividiyo kulesi sixhumanisi — mhlawumbe eyimfihlo, isusiwe, ivinjelwe isifunda, noma isixhumanisi sonakele.",
    "tiktok_generic_fail": "Ayikwazanga ukulandwa le vidiyo noma i-slideshow ku-TikTok. Zama esinye isixhumanisi noma phinda maduzane.",
    "tiktok_author": "Umbhali we-TikTok",
    "tiktok_music": "Umculo ovela ku-TikTok",
    "status_generating_image": "Kudweba isithombe",
    "status_voicing": "Kufundwa umbiko",
    "status_taking_longer": "Lokhu kuzothatha isikhathi eside…",
    "status_listening": "Ngilalele.",
    "pick_suffix": "\n\n(noma ubhale nje ngombiko)",
    "pick_expired": "Izinkinobho ziphelelwe yisikhathi — bhala ngombiko.",
    "pick_not_yours": "Lezi akuzona izinkinobho zakho.",
    "pick_choice": "Okukhethiwe: {choice}",
    "owner_quota_notice": "⚠️ I-quot ye-Gemini iphele ngokuphelele kuwo wonke amamodeli omzila (bona /stats ngemininingwane).",
}

# Реестр языковых паков волны 1+ (см. комментарий в шапке про STRINGS vs PACK).
LANG_PACKS: dict[str, dict[str, str]] = {
    "pt": PACK_PT,
    "ar": PACK_AR,
    "tr": PACK_TR,
    "de": PACK_DE,
    "fr": PACK_FR,
    "it": PACK_IT,
    "hi": PACK_HI,
    "id": PACK_ID,
    "ms": PACK_MS,
    "vi": PACK_VI,
    "th": PACK_TH,
    "fa": PACK_FA,
    "ur": PACK_UR,
    "uz": PACK_UZ,
    "bn": PACK_BN,
    "fil": PACK_FIL,
    "pl": PACK_PL,
    "nl": PACK_NL,
    "zu": PACK_ZU,
}

# ── Кнопки-уточнения: вопросы/варианты/шаблоны по языкам ──
# Структура сценария та же, что была в PICK_* (см. историю git): вопрос,
# 4 опции, шаблон дописки выбора к исходному запросу.
PICK_TABLE: dict[str, dict[str, dict[str, object]]] = {
    "be": {
        "film": {"q": "Чаго сёння хочацца?", "opts": ["Лёгкае і вясёлае", "Драма", "Трылер", "Фантастыка"], "tpl": "{original} (жанр: {choice})"},
        "series": {"q": "Чаго сёння хочацца?", "opts": ["Лёгкае і вясёлае", "Драма", "Дэтэктыў", "Фантастыка"], "tpl": "{original} (жанр: {choice})"},
        "music": {"q": "Які настрой?", "opts": ["Энергічнае", "Спакойнае", "Сумнае", "Вясёлае"], "tpl": "{original} (настрой: {choice})"},
        "books": {"q": "Чаго сёння хочацца?", "opts": ["Фантастыка", "Дэтэктыў", "Нон-фікшн", "Класіка"], "tpl": "{original} (жанр: {choice})"},
        "games": {"q": "У што хочацца?", "opts": ["Экшн", "Стратэгія", "RPG", "Галаваломка"], "tpl": "{original} (жанр: {choice})"},
    },
    "en": {
        "film": {"q": "What are you in the mood for?", "opts": ["Light and fun", "Drama", "Thriller", "Sci-fi"], "tpl": "{original} (genre: {choice})"},
        "series": {"q": "What are you in the mood for?", "opts": ["Light and fun", "Drama", "Detective", "Sci-fi"], "tpl": "{original} (genre: {choice})"},
        "music": {"q": "What mood?", "opts": ["Energetic", "Calm", "Sad", "Cheerful"], "tpl": "{original} (mood: {choice})"},
        "books": {"q": "What are you in the mood for?", "opts": ["Sci-fi", "Detective", "Non-fiction", "Classics"], "tpl": "{original} (genre: {choice})"},
        "games": {"q": "What do you feel like playing?", "opts": ["Action", "Strategy", "RPG", "Puzzle"], "tpl": "{original} (genre: {choice})"},
    },
    "es": {
        "film": {"q": "¿Qué te apetece hoy?", "opts": ["Algo ligero y divertido", "Drama", "Thriller", "Ciencia ficción"], "tpl": "{original} (género: {choice})"},
        "series": {"q": "¿Qué te apetece hoy?", "opts": ["Algo ligero y divertido", "Drama", "Detectives", "Ciencia ficción"], "tpl": "{original} (género: {choice})"},
        "music": {"q": "¿Qué ánimo?", "opts": ["Enérgica", "Tranquila", "Triste", "Alegre"], "tpl": "{original} (ánimo: {choice})"},
        "books": {"q": "¿Qué te apetece hoy?", "opts": ["Ciencia ficción", "Detectives", "No ficción", "Clásicos"], "tpl": "{original} (género: {choice})"},
        "games": {"q": "¿A qué quieres jugar?", "opts": ["Acción", "Estrategia", "RPG", "Puzles"], "tpl": "{original} (género: {choice})"},
    },
    "kk": {
        "film": {"q": "Бүгін не қалайсың?", "opts": ["Жеңіл әрі көңілді", "Драма", "Триллер", "Фантастика"], "tpl": "{original} (жанр: {choice})"},
        "series": {"q": "Бүгін не қалайсың?", "opts": ["Жеңіл әрі көңілді", "Драма", "Детектив", "Фантастика"], "tpl": "{original} (жанр: {choice})"},
        "music": {"q": "Көңіл күйің қандай?", "opts": ["Жігерлі", "Сабырлы", "Мұңды", "Көңілді"], "tpl": "{original} (көңіл күй: {choice})"},
        "books": {"q": "Бүгін не қалайсың?", "opts": ["Фантастика", "Детектив", "Нон-фикшн", "Классика"], "tpl": "{original} (жанр: {choice})"},
        "games": {"q": "Не ойнағың келеді?", "opts": ["Экшн", "Стратегия", "RPG", "Басқатырғыш"], "tpl": "{original} (жанр: {choice})"},
    },
    "ru": {
        "film": {"q": "Что сегодня хочется?", "opts": ["Лёгкое и весёлое", "Драма", "Триллер", "Фантастика"], "tpl": "{original} (жанр: {choice})"},
        "series": {"q": "Что сегодня хочется?", "opts": ["Лёгкое и весёлое", "Драма", "Детектив", "Фантастика"], "tpl": "{original} (жанр: {choice})"},
        "music": {"q": "Какое настроение?", "opts": ["Энергичное", "Спокойное", "Грустное", "Весёлое"], "tpl": "{original} (настроение: {choice})"},
        "books": {"q": "Что сегодня хочется?", "opts": ["Фантастика", "Детектив", "Нон-фикшн", "Классика"], "tpl": "{original} (жанр: {choice})"},
        "games": {"q": "Во что хочется?", "opts": ["Экшен", "Стратегия", "RPG", "Головоломка"], "tpl": "{original} (жанр: {choice})"},
    },
    "uk": {
        "film": {"q": "Чого сьогодні хочеться?", "opts": ["Легке і веселе", "Драма", "Трилер", "Фантастика"], "tpl": "{original} (жанр: {choice})"},
        "series": {"q": "Чого сьогодні хочеться?", "opts": ["Легке і веселе", "Драма", "Детектив", "Фантастика"], "tpl": "{original} (жанр: {choice})"},
        "music": {"q": "Який настрій?", "opts": ["Енергійне", "Спокійне", "Сумне", "Веселе"], "tpl": "{original} (настрій: {choice})"},
        "books": {"q": "Чого сьогодні хочеться?", "opts": ["Фантастика", "Детектив", "Нон-фікшн", "Класика"], "tpl": "{original} (жанр: {choice})"},
        "games": {"q": "У що хочеться?", "opts": ["Екшен", "Стратегія", "RPG", "Головоломка"], "tpl": "{original} (жанр: {choice})"},
    },
    "pt": {
        "film": {"q": "O que você quer hoje?", "opts": ["Leve e divertido", "Drama", "Thriller", "Ficção científica"], "tpl": "{original} (gênero: {choice})"},
        "series": {"q": "O que você quer hoje?", "opts": ["Leve e divertido", "Drama", "Detetive", "Ficção científica"], "tpl": "{original} (gênero: {choice})"},
        "music": {"q": "Qual o clima?", "opts": ["Energética", "Calma", "Triste", "Alegre"], "tpl": "{original} (clima: {choice})"},
        "books": {"q": "O que você quer hoje?", "opts": ["Ficção científica", "Detetive", "Não ficção", "Clássicos"], "tpl": "{original} (gênero: {choice})"},
        "games": {"q": "O que quer jogar?", "opts": ["Ação", "Estratégia", "RPG", "Quebra-cabeça"], "tpl": "{original} (gênero: {choice})"},
    },
    "ar": {
        "film": {"q": "ما الذي تريده اليوم؟", "opts": ["خفيف وممتع", "دراما", "إثارة", "خيال علمي"], "tpl": "{original} (النوع: {choice})"},
        "series": {"q": "ما الذي تريده اليوم؟", "opts": ["خفيف وممتع", "دراما", "بوليسي", "خيال علمي"], "tpl": "{original} (النوع: {choice})"},
        "music": {"q": "ما المزاج؟", "opts": ["نشيطة", "هادئة", "حزينة", "مرحة"], "tpl": "{original} (المزاج: {choice})"},
        "books": {"q": "ما الذي تريده اليوم؟", "opts": ["خيال علمي", "بوليسي", "غير روائي", "كلاسيكيات"], "tpl": "{original} (النوع: {choice})"},
        "games": {"q": "ماذا تريد أن تلعب؟", "opts": ["أكشن", "استراتيجية", "RPG", "ألغاز"], "tpl": "{original} (النوع: {choice})"},
    },
    "tr": {
        "film": {"q": "Bugün ne istersin?", "opts": ["Hafif ve eğlenceli", "Dram", "Gerilim", "Bilim kurgu"], "tpl": "{original} (tür: {choice})"},
        "series": {"q": "Bugün ne istersin?", "opts": ["Hafif ve eğlenceli", "Dram", "Dedektif", "Bilim kurgu"], "tpl": "{original} (tür: {choice})"},
        "music": {"q": "Modun ne?", "opts": ["Enerjik", "Sakin", "Hüzünlü", "Neşeli"], "tpl": "{original} (mod: {choice})"},
        "books": {"q": "Bugün ne istersin?", "opts": ["Bilim kurgu", "Dedektif", "Kurgu dışı", "Klasikler"], "tpl": "{original} (tür: {choice})"},
        "games": {"q": "Ne oynamak istersin?", "opts": ["Aksiyon", "Strateji", "RPG", "Bulmaca"], "tpl": "{original} (tür: {choice})"},
    },
    "de": {
        "film": {"q": "Worauf hast du heute Lust?", "opts": ["Leicht und lustig", "Drama", "Thriller", "Sci-Fi"], "tpl": "{original} (Genre: {choice})"},
        "series": {"q": "Worauf hast du heute Lust?", "opts": ["Leicht und lustig", "Drama", "Krimi", "Sci-Fi"], "tpl": "{original} (Genre: {choice})"},
        "music": {"q": "Welche Stimmung?", "opts": ["Energiegeladen", "Ruhig", "Traurig", "Fröhlich"], "tpl": "{original} (Stimmung: {choice})"},
        "books": {"q": "Worauf hast du heute Lust?", "opts": ["Sci-Fi", "Krimi", "Sachbuch", "Klassiker"], "tpl": "{original} (Genre: {choice})"},
        "games": {"q": "Worauf hast du Lust zu spielen?", "opts": ["Action", "Strategie", "RPG", "Puzzle"], "tpl": "{original} (Genre: {choice})"},
    },
    "fr": {
        "film": {"q": "Qu'est-ce qui te ferait plaisir aujourd'hui ?", "opts": ["Léger et drôle", "Drame", "Thriller", "Science-fiction"], "tpl": "{original} (genre : {choice})"},
        "series": {"q": "Qu'est-ce qui te ferait plaisir aujourd'hui ?", "opts": ["Léger et drôle", "Drame", "Policier", "Science-fiction"], "tpl": "{original} (genre : {choice})"},
        "music": {"q": "Quelle humeur ?", "opts": ["Énergique", "Calme", "Triste", "Joyeuse"], "tpl": "{original} (humeur : {choice})"},
        "books": {"q": "Qu'est-ce qui te ferait plaisir aujourd'hui ?", "opts": ["Science-fiction", "Policier", "Non-fiction", "Classiques"], "tpl": "{original} (genre : {choice})"},
        "games": {"q": "À quoi veux-tu jouer ?", "opts": ["Action", "Stratégie", "RPG", "Puzzle"], "tpl": "{original} (genre : {choice})"},
    },
    "it": {
        "film": {"q": "Cosa ti va oggi?", "opts": ["Leggero e divertente", "Dramma", "Thriller", "Fantascienza"], "tpl": "{original} (genere: {choice})"},
        "series": {"q": "Cosa ti va oggi?", "opts": ["Leggero e divertente", "Dramma", "Giallo", "Fantascienza"], "tpl": "{original} (genere: {choice})"},
        "music": {"q": "Che umore hai?", "opts": ["Energica", "Calma", "Triste", "Allegra"], "tpl": "{original} (umore: {choice})"},
        "books": {"q": "Cosa ti va oggi?", "opts": ["Fantascienza", "Giallo", "Saggistica", "Classici"], "tpl": "{original} (genere: {choice})"},
        "games": {"q": "A cosa vuoi giocare?", "opts": ["Azione", "Strategia", "RPG", "Puzzle"], "tpl": "{original} (genere: {choice})"},
    },
    "hi": {
        "film": {"q": "आज क्या चाहिए?", "opts": ["हल्का और मज़ेदार", "ड्रामा", "थ्रिलर", "साइंस फिक्शन"], "tpl": "{original} (शैली: {choice})"},
        "series": {"q": "आज क्या चाहिए?", "opts": ["हल्का और मज़ेदार", "ड्रामा", "जासूसी", "साइंस फिक्शन"], "tpl": "{original} (शैली: {choice})"},
        "music": {"q": "मूड कैसा है?", "opts": ["जोशीला", "शांत", "उदास", "खुश"], "tpl": "{original} (मूड: {choice})"},
        "books": {"q": "आज क्या चाहिए?", "opts": ["साइंस फिक्शन", "जासूसी", "नॉन-फिक्शन", "क्लासिक्स"], "tpl": "{original} (शैली: {choice})"},
        "games": {"q": "क्या खेलने का मन है?", "opts": ["एक्शन", "रणनीति", "RPG", "पहेली"], "tpl": "{original} (शैली: {choice})"},
    },
    "id": {
        "film": {"q": "Lagi ingin yang seperti apa?", "opts": ["Ringan dan seru", "Drama", "Thriller", "Fiksi ilmiah"], "tpl": "{original} (genre: {choice})"},
        "series": {"q": "Lagi ingin yang seperti apa?", "opts": ["Ringan dan seru", "Drama", "Detektif", "Fiksi ilmiah"], "tpl": "{original} (genre: {choice})"},
        "music": {"q": "Mood apa?", "opts": ["Enerjik", "Tenang", "Sedih", "Ceria"], "tpl": "{original} (mood: {choice})"},
        "books": {"q": "Lagi ingin yang seperti apa?", "opts": ["Fiksi ilmiah", "Detektif", "Nonfiksi", "Klasik"], "tpl": "{original} (genre: {choice})"},
        "games": {"q": "Mau main apa?", "opts": ["Aksi", "Strategi", "RPG", "Teka-teki"], "tpl": "{original} (genre: {choice})"},
    },
    "ms": {
        "film": {"q": "Hari ini nak yang macam mana?", "opts": ["Ringan dan seronok", "Drama", "Ngeri", "Fiksyen sains"], "tpl": "{original} (genre: {choice})"},
        "series": {"q": "Hari ini nak yang macam mana?", "opts": ["Ringan dan seronok", "Drama", "Detektif", "Fiksyen sains"], "tpl": "{original} (genre: {choice})"},
        "music": {"q": "Mood macam mana?", "opts": ["Bertenaga", "Tenang", "Sedih", "Ceria"], "tpl": "{original} (mood: {choice})"},
        "books": {"q": "Hari ini nak yang macam mana?", "opts": ["Fiksyen sains", "Detektif", "Bukan fiksyen", "Klasik"], "tpl": "{original} (genre: {choice})"},
        "games": {"q": "Nak main apa?", "opts": ["Aksi", "Strategi", "RPG", "Teka-teki"], "tpl": "{original} (genre: {choice})"},
    },
    "vi": {
        "film": {"q": "Hôm nay muốn gì?", "opts": ["Nhẹ nhàng vui vẻ", "Chính kịch", "Giật gân", "Khoa học viễn tưởng"], "tpl": "{original} (thể loại: {choice})"},
        "series": {"q": "Hôm nay muốn gì?", "opts": ["Nhẹ nhàng vui vẻ", "Chính kịch", "Trinh thám", "Khoa học viễn tưởng"], "tpl": "{original} (thể loại: {choice})"},
        "music": {"q": "Tâm trạng nào?", "opts": ["Sôi động", "Nhẹ nhàng", "Buồn", "Vui"], "tpl": "{original} (tâm trạng: {choice})"},
        "books": {"q": "Hôm nay muốn gì?", "opts": ["Khoa học viễn tưởng", "Trinh thám", "Phi hư cấu", "Kinh điển"], "tpl": "{original} (thể loại: {choice})"},
        "games": {"q": "Muốn chơi gì?", "opts": ["Hành động", "Chiến thuật", "RPG", "Giải đố"], "tpl": "{original} (thể loại: {choice})"},
    },
    "th": {
        "film": {"q": "วันนี้อยากได้แบบไหน?", "opts": ["เบา ๆ สนุก ๆ", "ดราม่า", "ระทึกขวัญ", "ไซไฟ"], "tpl": "{original} (แนว: {choice})"},
        "series": {"q": "วันนี้อยากได้แบบไหน?", "opts": ["เบา ๆ สนุก ๆ", "ดราม่า", "สืบสวน", "ไซไฟ"], "tpl": "{original} (แนว: {choice})"},
        "music": {"q": "อารมณ์ไหน?", "opts": ["มีพลัง", "สงบ", "เศร้า", "ร่าเริง"], "tpl": "{original} (อารมณ์: {choice})"},
        "books": {"q": "วันนี้อยากได้แบบไหน?", "opts": ["ไซไฟ", "สืบสวน", "สารคดี", "คลาสสิก"], "tpl": "{original} (แนว: {choice})"},
        "games": {"q": "อยากเล่นอะไร?", "opts": ["แอ็กชัน", "วางแผน", "RPG", "ปริศนา"], "tpl": "{original} (แนว: {choice})"},
    },
    "fa": {
        "film": {"q": "امروز چی می‌خوای؟", "opts": ["سبک و بامزه", "درام", "تریلر", "علمی‌تخیلی"], "tpl": "{original} (ژانр: {choice})"},
        "series": {"q": "امروز چی می‌خوای؟", "opts": ["سبک و بامزه", "درام", "پلیسی", "علمی‌تخیلی"], "tpl": "{original} (ژانر: {choice})"},
        "music": {"q": "حالت چطوره؟", "opts": ["پرانرژی", "آروم", "غمگین", "شاد"], "tpl": "{original} (حال: {choice})"},
        "books": {"q": "امروز چی می‌خوای؟", "opts": ["علمی‌تخیلی", "پلیسی", "غیرداستانی", "کلاسیک‌ها"], "tpl": "{original} (ژانر: {choice})"},
        "games": {"q": "چی بازی کنیم؟", "opts": ["اکشن", "استراتژی", "RPG", "معمایی"], "tpl": "{original} (ژانر: {choice})"},
    },
    "ur": {
        "film": {"q": "آج کیا چاہیے؟", "opts": ["ہلکا اور مزیدار", "ڈراما", "تھرلر", "سائنس فکشن"], "tpl": "{original} (صنف: {choice})"},
        "series": {"q": "آج کیا چاہیے؟", "opts": ["ہلکا اور مزیدار", "ڈراما", "جاسوسی", "سائنس فکشن"], "tpl": "{original} (صنف: {choice})"},
        "music": {"q": "موڈ کیسا ہے؟", "opts": ["جوشیلا", "پرسکون", "اداس", "خوش"], "tpl": "{original} (موڈ: {choice})"},
        "books": {"q": "آج کیا چاہیے؟", "opts": ["سائنس فکشن", "جاسوسی", "نان فکشن", "کلاسکس"], "tpl": "{original} (صنف: {choice})"},
        "games": {"q": "کیا کھیلنے کا من ہے؟", "opts": ["ایکشن", "حکمت عملی", "RPG", "پہیلی"], "tpl": "{original} (صنف: {choice})"},
    },
    "uz": {
        "film": {"q": "Bugun nima xohlaysan?", "opts": ["Yengil va quvnoq", "Drama", "Triller", "Fantastika"], "tpl": "{original} (janr: {choice})"},
        "series": {"q": "Bugun nima xohlaysan?", "opts": ["Yengil va quvnoq", "Drama", "Detektiv", "Fantastika"], "tpl": "{original} (janr: {choice})"},
        "music": {"q": "Kayfiyating qalay?", "opts": ["Shijoatli", "Sokin", "G'amgin", "Xursand"], "tpl": "{original} (kayfiyat: {choice})"},
        "books": {"q": "Bugun nima xohlaysan?", "opts": ["Fantastika", "Detektiv", "Non-fikshn", "Klassika"], "tpl": "{original} (janr: {choice})"},
        "games": {"q": "Nima o'ynagimiz keladi?", "opts": ["Ekshn", "Strategiya", "RPG", "Boshqotirma"], "tpl": "{original} (janr: {choice})"},
    },
    "bn": {
        "film": {"q": "আজ কী চাই?", "opts": ["হালকা ও মজার", "ড্রামা", "থ্রিলার", "সায়েন্স ফিকশন"], "tpl": "{original} (ধরন: {choice})"},
        "series": {"q": "আজ কী চাই?", "opts": ["হালকা ও মজার", "ড্রামা", "গোয়েন্দা", "সায়েন্স ফিকশন"], "tpl": "{original} (ধরন: {choice})"},
        "music": {"q": "মুড কেমন?", "opts": ["উদ্যমী", "শান্ত", "দুঃখী", "খুশি"], "tpl": "{original} (মুড: {choice})"},
        "books": {"q": "আজ কী চাই?", "opts": ["সায়েন্স ফিকশন", "গোয়েন্দা", "নন-ফিকশন", "ক্লাসিক"], "tpl": "{original} (ধরন: {choice})"},
        "games": {"q": "কী খেলতে মন চায়?", "opts": ["অ্যাকশন", "কৌশল", "RPG", "ধাঁধা"], "tpl": "{original} (ধরন: {choice})"},
    },
    "fil": {
        "film": {"q": "Ano'ng gusto mo ngayon?", "opts": ["Magaan at masaya", "Drama", "Thriller", "Sci-fi"], "tpl": "{original} (genre: {choice})"},
        "series": {"q": "Ano'ng gusto mo ngayon?", "opts": ["Magaan at masaya", "Drama", "Detective", "Sci-fi"], "tpl": "{original} (genre: {choice})"},
        "music": {"q": "Anong mood?", "opts": ["Energetic", "Kalmado", "Malungkot", "Masaya"], "tpl": "{original} (mood: {choice})"},
        "books": {"q": "Ano'ng gusto mo ngayon?", "opts": ["Sci-fi", "Detective", "Non-fiction", "Classics"], "tpl": "{original} (genre: {choice})"},
        "games": {"q": "Anong gustong laruin?", "opts": ["Action", "Strategy", "RPG", "Puzzle"], "tpl": "{original} (genre: {choice})"},
    },
    "pl": {
        "film": {"q": "Na co masz dziś ochotę?", "opts": ["Lekkie i zabawne", "Dramat", "Thriller", "Sci-fi"], "tpl": "{original} (gatunek: {choice})"},
        "series": {"q": "Na co masz dziś ochotę?", "opts": ["Lekkie i zabawne", "Dramat", "Kryminał", "Sci-fi"], "tpl": "{original} (gatunek: {choice})"},
        "music": {"q": "Jaki nastrój?", "opts": ["Energetyczny", "Spokojny", "Smutny", "Wesoły"], "tpl": "{original} (nastrój: {choice})"},
        "books": {"q": "Na co masz dziś ochotę?", "opts": ["Sci-fi", "Kryminał", "Non-fiction", "Klasyka"], "tpl": "{original} (gatunek: {choice})"},
        "games": {"q": "W co chcesz zagrać?", "opts": ["Akcja", "Strategia", "RPG", "Łamigłówki"], "tpl": "{original} (gatunek: {choice})"},
    },
    "nl": {
        "film": {"q": "Waar heb je vandaag zin in?", "opts": ["Licht en leuk", "Drama", "Thriller", "Sci-fi"], "tpl": "{original} (genre: {choice})"},
        "series": {"q": "Waar heb je vandaag zin in?", "opts": ["Licht en leuk", "Drama", "Detective", "Sci-fi"], "tpl": "{original} (genre: {choice})"},
        "music": {"q": "Welke stemming?", "opts": ["Energiek", "Kalm", "Droevig", "Vrolijk"], "tpl": "{original} (stemming: {choice})"},
        "books": {"q": "Waar heb je vandaag zin in?", "opts": ["Sci-fi", "Detective", "Non-fictie", "Klassiekers"], "tpl": "{original} (genre: {choice})"},
        "games": {"q": "Wat wil je spelen?", "opts": ["Actie", "Strategie", "RPG", "Puzzel"], "tpl": "{original} (genre: {choice})"},
    },
    "zu": {
        "film": {"q": "Ufuna ini namuhla?", "opts": ["Okulula nokumnandi", "Idrama", "I-thriller", "I-sci-fi"], "tpl": "{original} (uhlobo: {choice})"},
        "series": {"q": "Ufuna ini namuhla?", "opts": ["Okulula nokumnandi", "Idrama", "Ubushenxu", "I-sci-fi"], "tpl": "{original} (uhlobo: {choice})"},
        "music": {"q": "Umuzwa muni?", "opts": ["Onamandla", "Ozolile", "Odabukile", "Ojabulile"], "tpl": "{original} (umuzwa: {choice})"},
        "books": {"q": "Ufuna ini namuhla?", "opts": ["I-sci-fi", "Ubushenxu", "Okungelona i-fiction", "Amakh lasikhiwa"], "tpl": "{original} (uhlobo: {choice})"},
        "games": {"q": "Ufuna ukudlala ini?", "opts": ["Isenzo", "Isu", "RPG", "Iphazili"], "tpl": "{original} (uhlobo: {choice})"},
    },
}


def t(lang: str | None, key: str, **kwargs: object) -> str:
    """Строка key на языке lang (фолбэк — английский). Плейсхолдеры вида
    {error}/{choice}/{name} подставляются из kwargs. Новые языки лежат в
    LANG_PACKS (см. низ файла), старые шесть — в STRINGS выше."""
    lang = normalize_lang(lang)
    table = STRINGS.get(key, {})
    text = LANG_PACKS.get(lang, {}).get(key) or table.get(lang) or table.get(DEFAULT_LANG) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def pick_texts(lang: str | None, scenario: str) -> tuple[str, list[str], str]:
    """(вопрос, опции, шаблон) pick-сценария на языке lang."""
    lang = normalize_lang(lang)
    table = PICK_TABLE.get(lang) or PICK_TABLE[DEFAULT_LANG]
    rec = table.get(scenario) or PICK_TABLE[DEFAULT_LANG].get(scenario, {})
    return (
        str(rec.get("q", "")),
        [str(o) for o in rec.get("opts", [])],
        str(rec.get("tpl", "{original} ({choice})")),
    )
