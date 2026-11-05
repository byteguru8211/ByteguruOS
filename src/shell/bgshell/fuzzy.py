"""Незамутённый поиск для Guru Line.

Почему не `in`: в палитре человек печатает «бр» и ждёт браузер, а не
«совпадений нет». Поэтому три вещи в одном порядке приоритета:

1. точное совпадение начала строки — сильнее всего;
2. совпадение начала любого слова (для «Visual Studio Code» подойдёт «vscode»);
3. подпоследовательность символов в порядке («бр» → «б**р**аузер») — слабее,
   но именно она делает палитру полезной при опечатках и коротких запросах.

Отдельного модуля просит и тестирование: сравнение строк с кириллицей,
диакритикой и регистром — то, что в GUI не проверяется глазами.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Насколько ценнее начало слова, чем продолжение.
WORD_START_BONUS = 120
EXACT_PREFIX_BONUS = 400
SUBSEQUENCE_STEP_PENALTY = 4
# Строка без совпадения вообще не попадает в выдачу.
NO_MATCH = -1


@dataclass(frozen=True)
class Candidate:
    """Один результат палитры.

    keywords — то, по чему ищут, но не то, что показывают: «файлы» для
    Thunar, «терминал» для foot. Без них палитра умеет искать только по
    названиям из .desktop, а это ровно половина случаев.
    """

    ident: str
    title: str
    subtitle: str = ""
    keywords: tuple[str, ...] = ()
    kind: str = "app"
    payload: str = ""
    icon: str = ""
    score: int = field(default=0, compare=False)

    @property
    def haystack(self) -> str:
        return " ".join((self.title, *self.keywords, self.subtitle)).lower()


def _word_starts(text: str) -> list[int]:
    return [0] + [index + 1 for index, char in enumerate(text[:-1])
        if char == " "]


def score_term(term: str, text: str) -> int:
    """Оценка совпадения одного слова запроса со строкой."""
    if not term:
        return 0
    text = text.lower()
    term = term.lower()

    if text.startswith(term):
        return EXACT_PREFIX_BONUS + len(term)

    best = NO_MATCH
    for start in _word_starts(text):
        word = text[start:]
        if word.startswith(term):
            best = max(best, WORD_START_BONUS + len(term))
    if best != NO_MATCH:
        return best

    if term in text:
        return 60 + len(term)

    # Подпоследовательность: каждый следующий символ стоит дороже, чем
    # разрыв перед ним, поэтому «брау» ранжируется выше «б…р…у».
    index = 0
    matched = 0
    gaps = 0
    for char in text:
        if index < len(term) and char == term[index]:
            index += 1
            matched += 1
            gaps += 0
        elif matched:
            gaps += 1
    if index < len(term):
        return NO_MATCH
    return max(1, matched * 10 - gaps // SUBSEQUENCE_STEP_PENALTY)


def score(candidate: Candidate, query: str) -> int:
    """Итоговый балл: сумма по всем словам запроса, либо NO_MATCH."""
    terms = [term for term in query.lower().split() if term]
    if not terms:
        return 0
    total = 0
    haystack = candidate.haystack
    title = candidate.title.lower()
    for term in terms:
        # Лучшее из «по названию» и «по всем данным»: заголовок важнее
        # ключевых слов, и это видно по порядку слияния.
        total += max(score_term(term, title), score_term(term, haystack))
    return total


def rank(candidates: list[Candidate], query: str, limit: int = 12) -> list[Candidate]:
    """Сортирует по баллу, а при равенстве — по заголовку, чтобы выдача была
    стабильной между нажатиями клавиш."""
    if not query.strip():
        return sorted(candidates, key=lambda item: item.title.lower())[:limit]
    scored = []
    for candidate in candidates:
        value = score(candidate, query)
        if value > 0:
            scored.append((value, candidate))
    scored.sort(key=lambda pair: (-pair[0], pair[1].title.lower()))
    return [
        Candidate(ident=c.ident, title=c.title, subtitle=c.subtitle,
            keywords=c.keywords, kind=c.kind, payload=c.payload, score=s)
        for s, c in scored[:limit]
    ]
