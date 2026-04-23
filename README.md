# AI_Decomp_Tools

Dieses Repository buendelt Pipeline-Skripte und Werkzeuge zur semantischen Analyse, Codegenerierung und Verarbeitung von MIPS-Assembly, primaer ausgelegt fuer N64-Decompilation-Workflows. 

Das Repository besteht aus drei voneinander getrennten, aber ergaenzenden Tools:

## 1. CtoIDO
**MIPS Assembly Generator & Cleaner**

Dieses Tool wandelt C-Dateien in sauberen, kommentarfreien MIPS-Assembly-Code um. Es ist darauf ausgelegt, Code originalgetreu mit dem IDO-Compiler zu verarbeiten.
* **Pipeline:** Der Code durchlaeuft eine Kette aus Header-Sanitisierung, Präprozessor (gcc), Kompilierung (IDO) und Disassemblierung (spimdisasm).
* **Outputs:** Pro C-Datei werden zwei Artefakte generiert:
    * `ASM_Raw`: Ein zusammengesetzter Output aller Sektionen (`.text`, `.data`, `.rodata`, `.bss`).
    * `ASM_Cleaned`: Eine bereinigte Version ohne Kommentare, mit normalisierten Labels und ausgewerteten statischen Bit-Operationen.
* *Weitere Details im [CtoIDO Unterordner](CtoIDO/README.md).*

## 2. Synthetic_C_Generator
**Fuzzing-Engines fuer IDO 5.3 / MIPS**

Dieses Verzeichnis enthaelt Generatoren, die automatisiert C-Code produzieren, welcher garantiert mit dem IDO 5.3 Compiler zu validem MIPS-Assembly kompiliert. Die Tools dienen als Input-Lieferanten fuer das Training von Machine-Learning-Modellen (z. B. einer Dead-Code Reducer Pipeline).
* **Engines:** * Basis Csmith-Generator fuer einfache, kontrollierte Funktionen.
    * AST-Mutator (via pycparser) zur gezielten Injektion typischer Decompilation-Muster wie spezifische Switch-Cases oder Do-While-Schleifen.
    * Gepatchte YARPGen-Integration fuer komplexe Konstrukte und Pointer-Arithmetik.
* *Weitere Details im [Synthetic_C_Generator Unterordner](Synthetic_C_Generator/README.md).*

## 3. TechEnv_Builder (Work in Progress)
**MIPS Semantic Expert Extractor**

Ein in Entwicklung befindliches Analyse-Tool, das die Bruecke zwischen rohem MIPS-Assembly und high-level C-Semantik schlaegt.
* **Funktion:** Analysiert MIPS-Assembly-Dateien (IDO Output) und verknuepft sie mit C-Header-Definitionen, um tiefgehende semantische Metadaten zu extrahieren.
* **Features:** Rekonstruiert Register-Tracking, Stack-Frame-Layouts, Variablen-Typisierung und Call-Graphs. Es erkennt spezifische Compiler-Muster wie Tail-Calls und Blind-Saves.
* **Output-Format:** Generiert pro Datei ein dichtes, token-effizientes JSON (`Semantic Expert IR - v10`), das in statische Typ-Datenbanken (`env`) und per-Funktion Analysen (`funcs[]`) gegliedert ist.
* *Weitere Details im [TechEnv_Builder Unterordner](TechEnv_Builder/README.md).*