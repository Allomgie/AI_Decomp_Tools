#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MIPS Assembly Generator & Cleaner
=================================

Ein-Schritt-Pipeline: C-Quellcode → IDO-Compiler → spimdisasm → ASM_Raw (zusammengesetzt) + ASM_Cleaned (bereinigt).

Ordnerstruktur (relativ zum Skript):
    .
    ├── Input_C/              # Eingabe: .c Dateien
    ├── Input_C_headers/      # Eingabe: zugehörige .h Dateien
    ├── IDO_compiler/         # IDO Compiler Toolchain (cc, etc.)
    ├── ASM_Raw/              # Ausgabe: zusammengesetzter Raw-ASM (.text, .data, .rodata, .bss)
    ├── ASM_Cleaned/          # Ausgabe: bereinigter MIPS-ASM
    └── Failed/               # Ausgabe: fehlgeschlagene .c/.h Paare

Abhängigkeiten:
    - gcc (Präprozessor)
    - python3 -m spimdisasm
    - tqdm
"""

import os
import re
import shutil
import subprocess
import multiprocessing
from tqdm import tqdm

# =============================================================================
# KONFIGURATION
# =============================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Pfade
IDO_DIR       = os.path.join(BASE_DIR, "IDO_compiler")
IDO_CC        = os.path.join(IDO_DIR, "cc")
INPUT_C_DIR   = os.path.join(BASE_DIR, "Input_C")
INPUT_H_DIR   = os.path.join(BASE_DIR, "Input_C_headers")
OUTPUT_RAW_DIR = os.path.join(BASE_DIR, "ASM_Raw")
OUTPUT_CLEAN_DIR = os.path.join(BASE_DIR, "ASM_Cleaned")
FAILED_DIR    = os.path.join(BASE_DIR, "Failed")
TMP_BASE      = os.path.join(BASE_DIR, ".tmp_pipeline")

# Include-Pfade für den Präprozessor (anpassen falls nötig)
INCLUDE_DIRS = [
    os.path.join(BASE_DIR, "include"),
    os.path.join(BASE_DIR, "src"),
    os.path.join(BASE_DIR, "include", "PR"),
    os.path.join(BASE_DIR, "lib", "ultralib", "include"),
    os.path.join(BASE_DIR, "csmith_install", "include", "csmith-2.3.0"),
]

# Compiler-Flags
CPP_FLAGS = ["-E", "-P", "-xc", "-D_LANGUAGE_C", "-D_MIPS_SZLONG=32"]
IDO_FLAGS = ["-c", "-O2", "-mips2", "-G", "0", "-w"]

# Ghost-Call Firewall: Verbotene Muster (verschiebt Datei sofort nach Failed)
FORBIDDEN_PATTERNS = [
    r'\bSDL_', r'\bPy[A-Z]', r'\blinux\b', r'\bposix\b',
    r'\bWEXITSTATUS\b', r'\bsetpgid\b', r'\bsignal\(', r'\b_exit\b',
    r'\bgetpid\b', r'\bWIFEXITED\b', r'\bstrtol\b', r'\bfprint\b',
    r'\breadn\b'
]

# =============================================================================
# ASM CLEANUP FUNKTIONEN
# =============================================================================

def clean_asm(raw_asm):
    """
    Bereinigt rohen MIPS-Assembly-Output von spimdisasm.
    - Entfernt überflüssige Kommentare und Formatierung
    - Normalisiert Labels
    - Wertet statische Bit-Operationen (>> 16, & 0xFFFF) aus
    """
    cleaned = []
    lines = raw_asm.splitlines()

    # Pattern für Instruktionszeilen: /* addr hex */ instruction
    instr_pattern = re.compile(
        r"^\s*/\*\s*[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+([0-9A-Fa-f]{8})\s*\*/\s*(.*)$"
    )
    # Pattern für Daten-Direktiven
    data_pattern = re.compile(
        r"^\s*/\*\s*[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s*\*/\s*(\.[a-z]+\s+.*)$"
    )
    # Pattern für reine Hex-Zeilen
    hex_only_pattern = re.compile(r"^\s*/\*\s*([0-9A-Fa-f]+)\s*\*/\s*$")
    # Pattern für Bit-Operationen in Operanden
    bitop_pattern = re.compile(r"\((0x[0-9A-Fa-f]+)\s*(>>\s*16|&\s*0xFFFF)\)")

    def evaluate_bitops(match):
        val = int(match.group(1), 16)
        op = match.group(2)
        if ">>" in op:
            result = val >> 16
        else:
            result = val & 0xFFFF
        return f"0x{result:X}"

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # 1. Funktions-/Daten-Startpunkte
        if stripped.startswith(("glabel", "dlabel", ".section")):
            cleaned.append(stripped)
            i += 1
            continue

        # 2. Lokale Sprungmarken normalisieren
        if stripped.endswith(":"):
            cleaned.append(f"00000000 {stripped}")
            i += 1
            continue

        # 3. endlabel
        if stripped.startswith("endlabel"):
            cleaned.append(stripped)
            i += 1
            continue

        # 4. Instruktionen verarbeiten & Bit-Ops berechnen
        match = instr_pattern.match(line)
        if match:
            hex_code = match.group(1).lower()
            instr = match.group(2).strip()
            # Kommentare entfernen
            instr = instr.split("#")[0].split(";")[0].strip()
            # Bit-Operationen ersetzen
            instr = bitop_pattern.sub(evaluate_bitops, instr)

            if instr:
                cleaned.append(f"{hex_code} {instr}")
            i += 1
            continue

        # 5. Daten-Direktiven mit Hex-Code mergen
        data_match = data_pattern.match(line)
        if data_match:
            directive = data_match.group(1).strip()
            directive = directive.split("#")[0].split(";")[0].strip()
            directive = bitop_pattern.sub(evaluate_bitops, directive)

            hex_val = ""
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                hex_match = hex_only_pattern.match(next_line)
                if hex_match:
                    hex_val = hex_match.group(1).lower()
                    i += 1

            if directive:
                if hex_val:
                    cleaned.append(f"{hex_val} {directive}")
                else:
                    cleaned.append(directive)
            i += 1
            continue

        i += 1

    return "\n".join(cleaned)


# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def sanitize_headers_in_place(header_dir):
    """Bereinigt Header-Dateien für den IDO-Compiler."""
    bad_externs = re.compile(
        r'^\s*extern\s+[a-zA-Z0-9_* ]+\s+(TRUE|FALSE|NULL|nil|True|False)\s*(\(\))?\s*;\s*$',
        re.MULTILINE
    )
    if not os.path.exists(header_dir):
        return

    for filename in os.listdir(header_dir):
        if not filename.endswith(".h"):
            continue
        filepath = os.path.join(header_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            # Relative Includes korrigieren
            new_content = re.sub(
                r'#include\s+"[.][.]/[^"]*?([^/"]+\.h)"',
                r'#include "\1"',
                content
            )
            # Schlechte extern-Deklarationen entfernen
            new_content = bad_externs.sub('', new_content)

            if content != new_content:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(new_content)
        except Exception:
            pass


def merge_and_clean_sections(spim_out_dir, final_s_path):
    """
    Fasst die von spimdisasm erzeugten Sektions-Dateien zusammen
    und entfernt doppelte Header.
    """
    if not os.path.exists(spim_out_dir) or not os.path.isdir(spim_out_dir):
        return False

    header = [
        '.include "macro.inc"\n', '\n',
        '/* assembler directives */\n',
        '.set noat      /* allow manual use of $at */\n',
        '.set noreorder /* do not insert nops after branches */\n', '\n'
    ]

    merged_content = []
    found_any = False
    section_endings = [".rodata.s", ".data.s", ".bss.s", ".text.s"]
    files_in_dir = os.listdir(spim_out_dir)

    for ending in section_endings:
        matching_files = [f for f in files_in_dir if f.endswith(ending)]
        if not matching_files:
            continue

        found_any = True
        sec_path = os.path.join(spim_out_dir, matching_files[0])
        with open(sec_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for line in lines:
            stripped = line.strip()
            if stripped in ['.include "macro.inc"', '/* assembler directives */']:
                continue
            if stripped.startswith('.set noat') or stripped.startswith('.set noreorder'):
                continue
            merged_content.append(line)

        merged_content.append("\n")

    if not found_any:
        return False

    with open(final_s_path, "w", encoding="utf-8") as f:
        f.writelines(header)
        f.writelines(merged_content)

    shutil.rmtree(spim_out_dir, ignore_errors=True)
    return True


# =============================================================================
# WORKER
# =============================================================================

def process_single_file(args):
    """
    Verarbeitet eine einzelne C-Datei durch die komplette Pipeline:
    C → Präprozessor → IDO → spimdisasm → ASM_Raw + ASM_Cleaned
    """
    c_filepath, header_dir, output_raw_path, output_clean_path, failed_dir = args
    filename = os.path.basename(c_filepath)
    name_no_ext = os.path.splitext(filename)[0]
    header_path = os.path.join(header_dir, f"{name_no_ext}.h")

    # --- 1. GHOST CALL FIREWALL ---
    try:
        with open(c_filepath, "r", encoding="utf-8") as f:
            content = f.read()

        for pattern in FORBIDDEN_PATTERNS:
            if re.search(pattern, content):
                fail_c = os.path.join(failed_dir, filename)
                fail_h = os.path.join(failed_dir, f"{name_no_ext}.h")
                if os.path.exists(c_filepath):
                    shutil.move(c_filepath, fail_c)
                if os.path.exists(header_path):
                    shutil.move(header_path, fail_h)
                return (False, f"Ghost Call Firewall: '{pattern}' in {filename}")

    except Exception as e:
        return (False, f"Lesefehler bei {filename}: {str(e)}")

    # --- 2. TEMP-ORDNER & DATEIEN ---
    tmp_dir = os.path.join(TMP_BASE, f"tmp_{name_no_ext}")
    os.makedirs(tmp_dir, exist_ok=True)

    i_p = os.path.join(tmp_dir, f"{name_no_ext}.i")
    o_p = os.path.join(tmp_dir, f"{name_no_ext}.o")
    tmp_c = os.path.join(tmp_dir, filename)

    success = False
    error_detail = None

    try:
        # C-Datei in Temp kopieren und Includes bereinigen
        content = re.sub(
            r'#include\s+"[.][.]/[^"]*?([^/"]+\.h)"',
            r'#include "\1"',
            content
        )
        with open(tmp_c, "w", encoding="utf-8") as f:
            f.write(content)

        # --- 3. PRÄPROZESSOR (GCC) ---
        cmd_cpp = ["gcc"] + CPP_FLAGS
        for inc in INCLUDE_DIRS + [header_dir, BASE_DIR]:
            cmd_cpp.extend(["-I", inc])
        cmd_cpp += [tmp_c, "-o", i_p]

        res_cpp = subprocess.run(cmd_cpp, capture_output=True, text=True)
        if res_cpp.returncode != 0:
            raise Exception(f"GCC Fehler: {res_cpp.stderr}")

        # --- 4. KOMPILIERUNG (IDO) ---
        env = os.environ.copy()
        env["COMPILER_PATH"] = IDO_DIR
        env["LD_LIBRARY_PATH"] = f"{IDO_DIR}:{env.get('LD_LIBRARY_PATH', '')}"

        cmd_ido = [IDO_CC] + IDO_FLAGS + [i_p, "-o", o_p]
        res_ido = subprocess.run(cmd_ido, env=env, cwd=tmp_dir,
                                 capture_output=True, text=True)
        if res_ido.returncode != 0:
            raise Exception(f"IDO Fehler: {res_ido.stderr}")

        # --- 5. DISASSEMBLIERUNG (spimdisasm) ---
        spim_out_dir = os.path.join(tmp_dir, "spim_out")
        cmd_spim = ["python3", "-m", "spimdisasm", "elfObjDisasm", o_p, spim_out_dir]
        subprocess.run(cmd_spim, capture_output=True, text=True)

        raw_asm_path = os.path.join(tmp_dir, f"{name_no_ext}_raw.s")
        if not merge_and_clean_sections(spim_out_dir, raw_asm_path):
            raise Exception("Kein ausführbarer Code/Daten gefunden.")

        # --- 6. RAW ASM SPEICHERN ---
        with open(raw_asm_path, "r", encoding="utf-8") as f:
            raw_asm = f.read()

        with open(output_raw_path, "w", encoding="utf-8") as f:
            f.write(raw_asm)

        # --- 7. CLEANUP ---
        cleaned_asm = clean_asm(raw_asm)

        if cleaned_asm.strip():
            with open(output_clean_path, "w", encoding="utf-8") as f:
                f.write(cleaned_asm)
            success = True
        else:
            raise Exception("Cleanup ergab leere Ausgabe.")

    except Exception as e:
        error_detail = str(e)

    # --- 8. AUFRÄUMEN / FAILED HANDLING ---
    if not success:
        fail_c = os.path.join(failed_dir, filename)
        fail_h = os.path.join(failed_dir, f"{name_no_ext}.h")

        if os.path.exists(c_filepath):
            shutil.move(c_filepath, fail_c)
        if os.path.exists(header_path):
            shutil.move(header_path, fail_h)
        if os.path.exists(output_raw_path):
            os.remove(output_raw_path)
        if os.path.exists(output_clean_path):
            os.remove(output_clean_path)

        shutil.rmtree(tmp_dir, ignore_errors=True)
        return (False, f"Verschoben {filename}: {error_detail}")

    # Temp-Ordner löschen bei Erfolg
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return (True, None)


# =============================================================================
# MAIN
# =============================================================================

def main():
    # Verzeichnisse erstellen
    os.makedirs(OUTPUT_RAW_DIR, exist_ok=True)
    os.makedirs(OUTPUT_CLEAN_DIR, exist_ok=True)
    os.makedirs(FAILED_DIR, exist_ok=True)
    os.makedirs(TMP_BASE, exist_ok=True)

    if not os.path.exists(INPUT_C_DIR):
        print(f"Fehler: Eingabeordner '{INPUT_C_DIR}' nicht gefunden!")
        return
    if not os.path.exists(INPUT_H_DIR):
        print(f"Fehler: Header-Ordner '{INPUT_H_DIR}' nicht gefunden!")
        return
    if not os.path.exists(IDO_CC):
        print(f"Fehler: IDO Compiler nicht gefunden unter '{IDO_CC}'!")
        return

    # Header sanitisieren
    print("Bereinige Header-Dateien...")
    sanitize_headers_in_place(INPUT_H_DIR)

    # Aufgaben sammeln
    tasks = []
    for filename in sorted(os.listdir(INPUT_C_DIR)):
        if not filename.endswith(".c"):
            continue

        c_filepath = os.path.join(INPUT_C_DIR, filename)
        name_no_ext = os.path.splitext(filename)[0]
        output_raw_path = os.path.join(OUTPUT_RAW_DIR, f"{name_no_ext}.s")
        output_clean_path = os.path.join(OUTPUT_CLEAN_DIR, f"{name_no_ext}.s")

        tasks.append((c_filepath, INPUT_H_DIR, output_raw_path, output_clean_path, FAILED_DIR))

    if not tasks:
        print("Keine .c Dateien gefunden.")
        return

    cpu_count = multiprocessing.cpu_count()
    print(f"\nStarte Pipeline für {len(tasks)} Dateien ({cpu_count} Prozesse)...")
    print("=" * 60)

    success_count = 0
    failed_count = 0

    with multiprocessing.Pool(processes=cpu_count) as pool:
        with tqdm(total=len(tasks), desc="Verarbeite") as pbar:
            for success, err_msg in pool.imap_unordered(process_single_file, tasks):
                if success:
                    success_count += 1
                else:
                    failed_count += 1
                pbar.update(1)

    print("\n" + "=" * 60)
    print(f"Ergebnis: Erfolgreich: {success_count} | Fehlgeschlagen: {failed_count}")
    print(f"Raw:      {OUTPUT_RAW_DIR}")
    print(f"Cleaned:  {OUTPUT_CLEAN_DIR}")
    print(f"Fehler:   {FAILED_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()