"""
lumen_lang.py — язык системных сообщений бота (не ответов ИИ).

Что это: все ФИКСИРОВАННЫЕ строки, которые бот отправляет пользователю сам
(/start, подсказки, ошибки, статусы, кнопки-уточнения и их служебные реплики),
на 6 языках. Ответы ИИ не трогаем — модель отвечает на языке собеседника
(см. RESPONSE LANGUAGE в system_prompt.py).

Языки (по алфавиту кода — так пользователю проще искать в меню):
be (Беларуская), en (English, дефолт), es (Español), kk (Қазақша),
ru (Русский), uk (Українська).

Выбор хранится per-chat в состоянии чата (поле "lang", см. get_state в bot.py)
и переживает рестарты через тот же слой персистентности, что история
(см. lumen_state_storage.py). Старой записи без поля соответствует "en".

Правила добавления языка: добавить код в SUPPORTED_LANGS + LANG_NAMES,
добавить перевод КАЖДОГО ключа в STRINGS и PICK_TABLE (тест полноты
test_lang_covers_all_keys во всех языках это проверяет — пропущенный перевод
не уедет в прод). Обращение — неформальное "ты" там, где язык это различает.
"""

from __future__ import annotations

# Порядок — по алфавиту кода языка: в меню кнопки идут в этом порядке.
SUPPORTED_LANGS: tuple[str, ...] = ("be", "en", "es", "kk", "ru", "uk")

# Нативные названия для кнопок меню (без флагов: флаг ≠ язык, см. план i18n).
LANG_NAMES: dict[str, str] = {
    "be": "Беларуская",
    "en": "English",
    "es": "Español",
    "kk": "Қазақша",
    "ru": "Русский",
    "uk": "Українська",
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
}


def t(lang: str | None, key: str, **kwargs: object) -> str:
    """Строка key на языке lang (фолбэк — английский). Плейсхолдеры вида
    {error}/{choice}/{name} подставляются из kwargs."""
    lang = normalize_lang(lang)
    table = STRINGS.get(key, {})
    text = table.get(lang) or table.get(DEFAULT_LANG) or key
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
