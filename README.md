# AI Task Orchestrator (v2.0)

Stanowy orkiestrator zadań współpracujący z **Claude Code** oraz **Gemini CLI**.  
Automatyzuje pełny cykl wytwórczy: **Architektura → Implementacja → Review → Iteracja → Sukces**.

Narzędzie jest **całkowicie niezależne od projektu** — instalujesz je raz i używasz w dowolnym repozytorium lub katalogu.

---

## Jak to działa?

Orkiestrator zarządza trzema wyspecjalizowanymi rolami agentów AI:

1.  **Architekt (Claude)**: Analizuje codebase, tworzy szczegółowy plan zmian w formacie JSON oraz definiuje weryfikowalne kryteria akceptacji.
2.  **Programista (Gemini)**: Otrzymuje plan i modyfikuje pliki projektu. Gemini ma uprawnienia do edycji kodu, tworzenia nowych plików i usuwania starych. Po każdej iteracji tworzy `implementation_report.md`.
3.  **Reviewer (Claude)**: Sprawdza diff zmian względem kryteriów akceptacji. Jeśli wszystko jest gotowe — zatwierdza (`APPROVED`). Jeśli nie — zwraca zadanie do poprawki (`CHANGES_REQUESTED`) z konkretnymi uwagami.

---

## Instalacja

Aby używać orkiestratora jako globalnej komendy systemowej:

1. Sklonuj to repozytorium.
2. Zainstaluj pakiet w trybie edytowalnym (wymaga Python 3.11+):
   ```bash
   pip install -e .
   ```
   To doda komendy `orch` oraz `orch-monitor` do Twojego systemu.

---

## Szybki start

1.  **Wejdź do katalogu swojego projektu** (najlepiej repozytorium git).
2.  **Zainicjuj zadanie**:
    ```bash
    orch new "Zrefaktoruj parser w src/parser.py na podejście funkcyjne i dodaj testy"
    ```
    Orkiestrator automatycznie wykryje root projektu (przez Git lub CWD) i utworzy katalog `.orchestrator/` na dane. 
    *Wskazówka: Dodaj `.orchestrator/` do swojego `.gitignore`.*

3.  **Uruchom proces**:
    ```bash
    orch run TASK-XXXXXX
    ```

4.  **Monitoruj postęp**:
    W osobnym terminalu wpisz `orch-monitor`, aby widzieć status zadań na żywo.

---

## Główne funkcje

### 🧠 Inteligentne wykrywanie root projektu
Orkiestrator automatycznie lokalizuje główny katalog projektu za pomocą `git rev-parse --show-toplevel`. Dzięki temu możesz wywoływać komendy z dowolnego podkatalogu, a dane zawsze trafią do wspólnego folderu `.orchestrator/` w korzeniu projektu.

### 👤 Human-in-the-loop (`--human-review`)
Jeśli chcesz mieć pełną kontrolę, uruchom:
```bash
orch run TASK-XXXXXX --human-review
```
Orkiestrator zatrzyma się po każdej implementacji Gemini i zapyta Cię, czy rozwiązanie działa. Możesz wtedy ręcznie przetestować kod. Jeśli powiesz `fail`, Claude przeanalizuje Twój feedback i przygotuje plan naprawy dla Gemini.

### 📜 Historia i audyt (Git)
Jeśli projekt jest repozytorium Git, orkiestrator po każdej iteracji robi automatyczny commit z opisem. Pozwala to na łatwy powrót do dowolnego etapu pracy agenta.

---

## Komendy CLI

| Komenda | Opis |
|:---|:---|
| `orch new "opis"` | Tworzy nowe zadanie i nadaje mu ID. |
| `orch run ID` | Uruchamia pętlę agentów dla danego zadania. |
| `orch run ID --human-review` | Uruchamia zadanie z Twoją weryfikacją po drodze. |
| `orch status` | Wyświetla listę zadań w aktualnym projekcie. |
| `orch status ID` | Wyświetla szczegółowy status i historię konkretnego zadania. |
| `orch reset ID` | Czyści historię i przywraca zadanie do stanu NEW (zachowując opis). |
| `orch-monitor` | Otwiera terminalowy dashboard (Live View). |

---

## Struktura danych (.orchestrator/)

W każdym projekcie powstaje izolowany katalog z danymi:
```
.orchestrator/
├── orchestrator.db     ← Baza SQLite (zadania, historia, statusy)
└── runs/               ← Logi i artefakty per-zadanie
    └── TASK-XXXXXX/
        ├── conversation.md    ← Pełny zapis "myśli" i decyzji agentów
        ├── state.json         ← Stan maszyny stanów
        ├── architect_plan.json
        └── review_iter_N.json
```

---

## Konfiguracja (Zmienne Env)

Możesz nadpisać domyślne ustawienia:
- `ORCH_MAX_ITERATIONS`: Limit rund (domyślnie 6).
- `ORCH_ARCHITECT_ROLE`: Model dla architekta (`claude` | `gemini`).
- `ORCH_DEVELOPER_ROLE`: Model dla programisty (`claude` | `gemini`).
- `ORCH_USE_GIT`: Czy robić automatyczne commity (domyślnie true).

Przykład:
```bash
ORCH_MAX_ITERATIONS=3 orch run TASK-XXXXXX
```
