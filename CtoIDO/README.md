# MIPS Assembly Generator & Cleaner

> C-Quellcode → IDO-Compiler → spimdisasm → ASM_Raw (zusammengesetzt) + ASM_Cleaned (bereinigt)

## Übersicht

Dieses Tool wandelt C-Dateien in sauberen, kommentarfreien MIPS-Assembly-Code um. Es erzeugt **zwei Ausgaben pro Datei**:

1. **ASM_Raw/** – Der zusammengesetzte Raw-Output von spimdisasm (alle Sektionen in einer Datei: `.text`, `.data`, `.rodata`, `.bss`).
2. **ASM_Cleaned/** – Die bereinigte Version mit entfernten Kommentaren, normalisierten Labels und ausgewerteten Bit-Operationen.

## Ordnerstruktur

```
.
├── pipeline.py             # Hauptskript
├── IDO_compiler/           # IDO Compiler Toolchain (cc, etc.)
├── Input_C/                # Eingabe: .c Dateien
├── Input_C_headers/        # Eingabe: zugehörige .h Dateien
├── ASM_Raw/                # Ausgabe: zusammengesetzter Raw-ASM
├── ASM_Cleaned/            # Ausgabe: bereinigter MIPS-ASM
├── Failed/                 # Fehlgeschlagene .c/.h Paare
└── .tmp_pipeline/          # Temporäre Dateien (automatisch gelöscht)
```

## Voraussetzungen

- **gcc** – für den C-Präprozessor
- **IDO Compiler** – legen Sie die IDO-Toolchain in den Ordner `IDO_compiler/`
- **spimdisasm** – `pip install spimdisasm`
- **tqdm** – `pip install tqdm`

## Installation

```bash
# Abhängigkeiten installieren
pip install -r requirements.txt

# IDO Compiler platzieren
# Kopieren Sie Ihre IDO-Toolchain nach ./IDO_compiler/
# Stellen Sie sicher, dass ./IDO_compiler/cc existiert und ausführbar ist.
```

## Verwendung

1. Legen Sie Ihre `.c` Dateien in `Input_C/`.
2. Legen Sie die zugehörigen `.h` Dateien in `Input_C_headers/`.
3. Führen Sie das Skript aus:

```bash
python3 pipeline.py
```

4. Die Ausgaben landen in:
   - `ASM_Raw/` – Zusammengesetzter Raw-ASM mit allen Sektionen
   - `ASM_Cleaned/` – Bereinigte, kompakte Version
5. Fehlgeschlagene Dateien werden nach `Failed/` verschoben (inkl. Fehlermeldung).

## Was macht das Skript?

1. **Ghost-Call Firewall** – Prüft auf verbotene Systemaufrufe (SDL, POSIX, etc.) und filtert diese Dateien sofort aus.
2. **Header-Sanitisierung** – Bereinigt relative Includes und ungültige `extern`-Deklarationen.
3. **Präprozessor (gcc)** – Expandiert Makros und Includes.
4. **Kompilierung (IDO)** – Erzeugt MIPS-ELF-Objektdateien mit originalgetreuen N64-Optimierungen (`-O2 -mips2 -G0`).
5. **Disassemblierung (spimdisasm)** – Wandelt die Objektdatei in lesbaren Assembly-Code um.
6. **Sektions-Merge** – Fasst `.text`, `.data`, `.rodata` und `.bss` in eine einzige `.s`-Datei zusammen (→ `ASM_Raw/`).
7. **Cleanup** –
   - Entfernt Adress-/Hex-Kommentare
   - Normalisiert Labels
   - Wertet statische Bit-Operationen (`>> 16`, `& 0xFFFF`) direkt aus
   - Entfernt überflüssige Assembler-Direktiven
   (→ `ASM_Cleaned/`)

## Konfiguration

Die wichtigsten Einstellungen befinden sich am Anfang von `pipeline.py`:

| Variable | Beschreibung |
|----------|-------------|
| `BASE_DIR` | Basisverzeichnis (standardmäßig das Skript-Verzeichnis) |
| `IDO_DIR` / `IDO_CC` | Pfad zum IDO-Compiler |
| `INCLUDE_DIRS` | Zusätzliche Include-Pfade für den Präprozessor |
| `CPP_FLAGS` / `IDO_FLAGS` | Compiler-Flags |
| `FORBIDDEN_PATTERNS` | Regex-Muster für die Ghost-Call Firewall |

## Fehlerbehebung

**"IDO Compiler nicht gefunden"**
→ Stellen Sie sicher, dass `IDO_compiler/cc` existiert und ausführbar ist (`chmod +x IDO_compiler/cc`).

**"GCC Fehler"**
→ Prüfen Sie, ob alle benötigten Header in `Input_C_headers/` oder in den `INCLUDE_DIRS` vorhanden sind.

**Dateien in `Failed/`**
→ Die Original-Dateien wurden dorthin verschoben. Überprüfen Sie die Fehlermeldung, korrigieren Sie den Code und verschieben Sie die Dateien zurück nach `Input_C/` bzw. `Input_C_headers/`.

## Lizenz

Dieses Projekt ist ein privates Tool zur Verarbeitung von N64-Assembly. Verwendung auf eigene Verantwortung.
