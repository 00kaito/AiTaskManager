# AI Task Orchestrator

Stanowy orkiestrator zadań dla Claude Code + Gemini CLI.  
Automatyzuje pętlę: **architektura → implementacja → review → iteracja → APPROVED**.

```
NEW → ARCHITECTING (Claude) → IMPLEMENTING (Gemini) → REVIEWING (Claude)
                                    ↑                        |
                                    └─── CHANGES_REQUESTED ──┘
                                                             |
                                                        APPROVED / STUCK / FAILED
```

---

## Wymagania

- Python 3.11+
- `claude` CLI — zainstalowany i zalogowany (`claude --version`)
- `gemini` CLI — zainstalowany i zalogowany (`gemini --version`)
- `git` — projekt musi być repozytorium git (opcjonalne, ale zalecane)

```bash
pip install -r requirements.txt
```

---

## Szybki start

```bash
# 1. Utwórz zadanie
python runner.py new "Zrefaktoruj moduł parsera — zamień klasę LegacyParser na funkcję parse_v2 przyjmującą List[str] i zwracającą List[dict]"

# Output:
# ✅ Task created: TASK-3A1F2B
#    Run with: python runner.py run TASK-3A1F2B

# 2. Uruchom orkiestrator
python runner.py run TASK-3A1F2B

# 3. Sprawdź status (w innym terminalu)
python monitor.py TASK-3A1F2B

# 4. Lista wszystkich zadań
python runner.py status
```

---

## Struktura projektu

```
orchestrator/
├── runner.py           ← główny skrypt (CLI entry point)
├── config.py           ← centralna konfiguracja
├── state.py            ← model danych + SQLite repository
├── agents.py           ← wrappery Claude CLI i Gemini CLI
├── prompts.py          ← szablony promptów dla każdej fazy
├── monitor.py          ← live dashboard w terminalu
├── requirements.txt
└── runs/
    └── TASK-XXXXXX/
        ├── task.md                   ← opis zadania
        ├── state.json                ← aktualny stan
        ├── architect_plan.json       ← plan Claude'a
        ├── review_iter_N.json        ← wynik review per iteracja
        └── orchestrator.log          ← logi
```

> `implementation_report.md` — Gemini zapisuje go zawsze w **katalogu głównym projektu** (nie w `runs/`), skąd Claude go odczytuje podczas review.

---

## Konfiguracja

Edytuj `config.py` lub użyj zmiennych środowiskowych:

| Zmienna env              | Domyślna wartość | Opis                                    |
|--------------------------|------------------|-----------------------------------------|
| `ORCH_MAX_ITERATIONS`    | `6`              | Maks. rund przed STUCK                  |
| `ORCH_CLAUDE_TIMEOUT`    | `300`            | Timeout Claude w sekundach              |
| `ORCH_GEMINI_TIMEOUT`    | `600`            | Timeout Gemini w sekundach              |
| `ORCH_RUNS_DIR`          | `runs/`          | Katalog z artefaktami                   |
| `ORCH_DB_PATH`           | `orchestrator.db`| Ścieżka do bazy SQLite                  |
| `ORCH_USE_GIT`           | `true`           | Czy robić git diff między iteracjami    |

```bash
ORCH_MAX_ITERATIONS=3 ORCH_USE_GIT=false python runner.py run TASK-XXXXXX
```

---

## Jak to działa — szczegóły każdej fazy

### 1. ARCHITECTING (Claude)

Claude otrzymuje:
- Opis zadania
- Drzewo plików projektu + treść kluczowych plików (do 60k znaków łącznie, per-plik do 8k)

Claude zwraca **JSON** z:
- `plan` — lista kroków implementacji z typem zmiany (`CREATE` / `MODIFY` / `DELETE`)
- `acceptance_criteria` — lista **weryfikowalnych** kryteriów (co dokładnie musi być zrobione)
- `risks` — potencjalne problemy

Na starcie tej fazy orkiestrator zapisuje aktualny **git SHA** (`task_start_sha`) — używany później do pełnego diffa w review.

### 2. IMPLEMENTING (Gemini)

Gemini otrzymuje:
- Opis zadania + plan architekta
- Listę **nieukończonych** kryteriów z poprzedniego review
- Git diff z poprzedniej iteracji (dla kontekstu delta)

Gemini ma **wyraźny nakaz edytowania plików bezpośrednio** narzędziami (nie wypisywania kodu w konsoli) i zapisuje raport postępu do `implementation_report.md` w katalogu głównym projektu.

Po każdej iteracji orkiestrator robi automatyczny `git commit` — dla audytu i izolacji zmian między rundami.

### 3. REVIEWING (Claude)

Claude otrzymuje:
- Listę kryteriów do weryfikacji
- Raport Gemini (`implementation_report.md`)
- **Pełny diff od początku taska** (`git diff <task_start_sha>..HEAD`) — Claude widzi wszystkie zmiany ze wszystkich iteracji, nie tylko ostatniej

Claude zwraca **JSON** z oceną każdego kryterium:
- `status`: `DONE` / `PENDING` / `FAILED`
- `evidence`: konkretna linijka/funkcja jako dowód
- `confidence`: `HIGH` / `MEDIUM` / `LOW`

### Wykrywanie "stuck"

Stuck detection mierzy diff **bieżącej iteracji** (nie od startu taska) — sprawdza realny postęp Gemini w danej rundzie. Jeśli dwie iteracje z rzędu brak zmian → `STUCK`.

---

## Stuck detection i limity

```
max_iterations = 6     # maks. iteracji pętli
max_stuck_rounds = 2   # ile iteracji bez diff zanim STUCK
min_diff_lines = 1     # min. zmiana linii żeby nie być "stuck"
```

Po `STUCK` — sprawdź `runs/TASK-XXXXXX/` ręcznie.  
Najczęstsze przyczyny: niejasny opis zadania lub zbyt duży zakres.

**Wskazówka:** Dziel duże taski na mniejsze. Jedno zadanie = jeden moduł / jeden feature.

---

## Monitor

```bash
# Live dashboard (wszystkie zadania)
python monitor.py

# Szczegóły jednego zadania
python monitor.py TASK-3A1F2B

# Jednorazowy print (do CI/CD)
python monitor.py --once
```

Wymaga `rich` dla kolorowego widoku (`pip install rich`). Działa też bez niego (plain text fallback).

---

## Testy

```bash
pytest tests/ -v

# Z timeoutem (na wypadek zawieszenia)
pytest tests/ -v --timeout=30
```

Testy nie wymagają połączenia z Claude ani Gemini — agenty są mockowane. 26 testów pokrywa: state/SQLite, parsowanie JSON, szablony promptów i pełny flow orkiestratora ze wszystkimi ścieżkami (APPROVED, STUCK, FAILED).

---

## Przykładowe dobre opisy zadań

```
✅ DOBRE (konkretne, weryfikowalne):
"Zamień funkcję process_data() w src/processor.py tak, żeby przyjmowała
 parametr batch_size: int = 100 i przetwarzała dane w partiach zamiast
 naraz. Dodaj testy w tests/test_processor.py."

❌ ZŁE (zbyt ogólne):
"Popraw kod parsera żeby był czystszy i szybszy."
```

---

## Znane ograniczenia

1. **Gemini CLI API** — wrapper zakłada flagi `--prompt` i `--yolo`. Dostosuj `agents.py` do swojej wersji CLI (`gemini --help`).
2. **Równoległość** — jedna instancja na raz. SQLite nie wspiera concurrent writes bez dodatkowej konfiguracji.
3. **Brak git** — przy `ORCH_USE_GIT=false` stuck detection i pełny diff w review są wyłączone. Claude ocenia wtedy wyłącznie na podstawie raportu Gemini.

---

## Changelog

### v1.1
- **Fix:** ujednolicono ścieżkę `implementation_report.md` — zawsze w katalogu głównym projektu (wcześniej prompt wskazywał inny katalog niż runner.py szukał)
- **Fix:** prompt Gemini zawiera wyraźny nakaz edycji plików narzędziami — eliminuje ryzyko że Gemini wypisze kod w konsoli zamiast go zapisać
- **Improvement:** limit kontekstu codebase zwiększony z 8k do 60k znaków (per-plik z 2k do 8k) — Claude architekt widzi więcej kodu
- **Improvement:** review Claude dostaje pełny diff od startu taska (`git diff <task_start_sha>..HEAD`) zamiast tylko diff z ostatniej iteracji — eliminuje ryzyko przeoczenia regresji między rundami

---

## Rozszerzenia (TODO)

- [ ] Slack/email powiadomienie przy APPROVED / STUCK
- [ ] Web UI do podglądu zadań
- [ ] Webhook trigger (zamiast CLI `new`)
- [ ] Parallel tasks (multiple tasks simultaneously)
- [ ] Custom agent per task type (różne modele do różnych zadań)
