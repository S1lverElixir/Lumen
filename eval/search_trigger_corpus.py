"""Замер триггера веб-поиска на живом корпусе формулировок.

Базовый замер 11.11.2026 (прод, скрины владельца из группы): из 35 вопросов, где
Claude ищет, бот искал только 18 — «когда вышла Fable 5.2» ушло в память.
Корпус не тест: это линейка, по которой судят правку детекта.

Запуск:  python eval/search_trigger_corpus.py
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import lumen_message_core as M

SHOULD_SEARCH = [
    "когда вышла Fable 5.2",
    "когда вышла модель claude Fable 5.2",
    "Fable 5.2 вышла?",
    "вышел ли Windows 12",
    "когда выйдет iPhone 19 Pro",
    "сколько серий в новом сезоне The Bear",
    "что нового в Minecraft",
    "какая сейчас версия Blender",
    "GPT-6 уже вышел",
    "кто выиграл Лигу наций",
    "результаты выборов в Румынии",
    "кто главный в NVIDIA",
    "сколько стоит bitcoin прямо сейчас",
    "какая ключевая ставка в России",
    "погода в Гомеле сегодня",
    "курс доллара к рублю",
    "расскажи про Silent Hill f",
    "что за модель Claude Sonnet 5",
    "какой процессор лучше купить в 2026",
    "вышел ли финал сериала",
    "премьера нового сезона когда",
    "дата выхода GTA 6",
    "is Fable 5.2 out",
    "when did Windows 12 release",
    "who is the CEO of Intel now",
    "latest Blender version",
    "what is the current price of gold",
    "кто канцлер Германии",
    "премьер-министр Великобритании сейчас",
    "покажи последние новости про Яндекс",
    "обновление прошивки на телефоне вышло",
    "какой телефон лучше купить сейчас",
    "сколько эпизодов во втором сезоне",
    "есть ли уже Claude 5",
    "забастовка актёров закончилась",
]

SHOULD_NOT_SEARCH = [
    "объясни, что такое митохондрия",
    "сколько будет 45*17",
    "напиши стих про осень",
    "переведи на немецкий: доброе утро",
    "исправь этот код",
    "что такое REST API",
    "почему небо синее",
    "расскажи про Вторую мировую войну",
    "как приготовить борщ",
    "помнишь, мы вчера обсуждали фильмы",
    "какая у тебя версия",
    "напиши письмо другу",
    "придумай имя котянке",
    "что такое теорема Пифагора",
    "перечисли столицы стран Южной Америки",
    "как работает трансформер в ML",
    "сравни Python и C++ по синтаксису",
    "напиши regex для email",
    "сочини шутку про понедельники",
    "объясни разницу между TCP и UDP",
]


def main() -> int:
    miss = [q for q in SHOULD_SEARCH if not M._wants_web_search(q)]
    false_pos = [q for q in SHOULD_NOT_SEARCH if M._wants_web_search(q)]
    print(f"должны искать: {len(SHOULD_SEARCH)}, промахов: {len(miss)}")
    for q in miss:
        print("   MISS:", q)
    print(f"не должны: {len(SHOULD_NOT_SEARCH)}, ложных: {len(false_pos)}")
    for q in false_pos:
        print("   FALSE:", q)
    return 1 if (miss or false_pos) else 0


if __name__ == "__main__":
    raise SystemExit(main())
