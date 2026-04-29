# AI Task Orchestrator

Stanowy orkiestrator zadań dla Claude Code + Gemini CLI.  
Automatyzuje pętlę: **architektura → implementacja → review → iteracja → APPROVED**.

Orkiestrator jest teraz **niezależny od projektu** — możesz go zainstalować raz i używać w dowolnym repozytorium git lub katalogu.

---

## Instalacja

Aby móc używać orkiestratora globalnie:

1. Sklonuj to repozytorium.
2. Zainstaluj pakiet w trybie edytowalnym (wymaga Python 3.11+):
   ```bash
   pip install -e .
   ```
   To doda komendy `orch` oraz `orch-monitor` do Twojego systemu.

---

## Szybki start

1. Wejdź do katalogu swojego projektu (najlepiej repozytorium git).
2. Zainicjuj nowe zadanie:
   ```bash
   orch new "Opisz co agent ma zrobić"
   ```
   Orkiestrator utworzy katalog `.orchestrator/` w root projektu (wykrytym przez git lub CWD), gdzie będzie trzymał bazę danych i logi. 
   **Pamiętaj, aby dodać `.orchestrator/` do swojego `.gitignore`.**

3. Uruchom zadanie:
   ```bash
   orch run TASK-XXXXXX
   ```

4. (Opcjonalnie) Śledź postęp w osobnym terminalu:
   ```bash
   orch-monitor
   ```

---

## Tryby pracy

### Tryb standardowy

```
NEW → ARCHITECTING (Claude) → IMPLEMENTING (Gemini) → REVIEWING (Claude)
                                    ↑                        |
                                    └─── CHANGES_REQUESTED ──┘
                                                             |
                                                        APPROVED / STUCK / FAILED
```

### Tryb `--human-review`

Zatrzymuje się po każdej implementacji, abyś mógł ręcznie przetestować kod.

```bash
orch run TASK-XXXXXX --human-review
```

---

## Użycie CLI

### Tworzenie zadania
```bash
orch new "Zrefaktoruj moduł parsera — zamień klasę LegacyParser na funkcję parse_v2..."
```

### Status zadań
```bash
orch status         # Lista wszystkich zadań w aktualnym projekcie
orch status TASK-ID # Szczegóły konkretnego zadania
```

### Resetowanie zadania
Jeśli chcesz zacząć od nowa (np. po zmianie opisu):
```bash
orch reset TASK-ID
```

### Nadpisanie roli agenta
```bash
orch run TASK-ID --architect=gemini --developer=claude
```

---

## Struktura danych (.orchestrator/)

W każdym projekcie, w którym użyjesz orkiestratora, powstanie katalog:
```
.orchestrator/
├── orchestrator.db     ← baza SQLite dla tego projektu
└── runs/
    └── TASK-XXXXXX/
        ├── conversation.md    ← zapis "myśli" agentów
        ├── state.json         ← stan maszyny stanów
        ├── architect_plan.json
        └── review_iter_N.json
```

---

## Konfiguracja

Możesz użyć zmiennych środowiskowych, aby zmienić zachowanie orkiestratora:

| Zmienna env              | Domyślna wartość | Opis                                    |
|--------------------------|------------------|-----------------------------------------|
| `ORCH_MAX_ITERATIONS`    | `6`              | Maks. rund przed STUCK                  |
| `ORCH_USE_GIT`           | `true`           | Czy robić git diff i auto-commity       |
| `ORCH_ARCHITECT_ROLE`    | `claude`         | Model dla architektury                  |
| `ORCH_DEVELOPER_ROLE`    | `gemini`         | Model dla implementacji                 |

---

## Testy

```bash
pytest test_orchestrator.py -v
```
Testy używają mocków, nie wymagają kluczy API.

---

## Changelog (v2.0)

- **Feature:** Całkowita niezależność od projektu — instalacja przez `pip install -e .`.
- **Feature:** Automatyczne wykrywanie root projektu przez git (`git rev-parse --show-toplevel`).
- **Feature:** Przechowywanie danych w ukrytym katalogu `.orchestrator/` wewnątrz projektu.
- **Feature:** Nowe komendy CLI: `orch` oraz `orch-monitor`.
- **Improvement:** Uproszczone zarządzanie ścieżkami w `config.py`.
