#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
0_asm_cleanup_raw_recursive.py
==============================
Cleans MIPS assembly files recursively: removes comments, normalizes formatting,
and evaluates static bit operations (>> 16, & 0xFFFF) directly within instructions.

Reads from:  INPUT_DIR  (with subdirectories)
Writes to:   OUTPUT_DIR (mirrors input structure)
"""
import os
import re
from tqdm import tqdm

# --- CONFIGURATION ---
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR  = os.path.join(BASE_DIR, "Input_ASM_Raw")
OUTPUT_DIR = os.path.join(BASE_DIR, "Output_ASM")


def clean_asm(raw_asm):
    """
    Cleans MIPS assembly and evaluates static bit operations
    like >> 16 and & 0xFFFF directly within instructions.
    """
    cleaned = []
    lines = raw_asm.splitlines()

    instr_pattern = re.compile(
        r"^\s*/\*\s*[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+([0-9A-Fa-f]{8})\s*\*/\s*(.*)$")
    data_pattern = re.compile(
        r"^\s*/\*\s*[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s*\*/\s*(\.[a-z]+\s+.*)$")
    hex_only_pattern = re.compile(
        r"^\s*/\*\s*([0-9A-Fa-f]+)\s*\*/\s*$")
    bitop_pattern = re.compile(
        r"\((0x[0-9A-Fa-f]+)\s*(>>\s*16|&\s*0xFFFF)\)")

    def evaluate_bitops(match):
        val = int(match.group(1), 16)
        op  = match.group(2)
        if ">>" in op:
            result = val >> 16
        else:
            result = val & 0xFFFF
        return f"0x{result:X}"

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped_line = line.strip()

        if stripped_line.startswith(("glabel", "dlabel", ".section")):
            cleaned.append(stripped_line)
            i += 1
            continue

        if stripped_line.endswith(":"):
            cleaned.append(f"00000000 {stripped_line}")
            i += 1
            continue

        if stripped_line.startswith("endlabel"):
            cleaned.append(stripped_line)
            i += 1
            continue

        match = instr_pattern.match(line)
        if match:
            hex_code = match.group(1).lower()
            instr = match.group(2).strip()
            instr = instr.split("#")[0].split(";")[0].strip()
            instr = bitop_pattern.sub(evaluate_bitops, instr)
            if instr:
                cleaned.append(f"{hex_code} {instr}")
            i += 1
            continue

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


def main():
    if not os.path.exists(INPUT_DIR):
        print(f"Error: Input directory '{INPUT_DIR}' not found!")
        return

    raw_dirs = sorted([
        d for d in os.listdir(INPUT_DIR)
        if os.path.isdir(os.path.join(INPUT_DIR, d))
    ])

    if not raw_dirs:
        print(f"No subdirectories found in '{INPUT_DIR}'!")
        return

    print(f"Found groups: {len(raw_dirs)}")
    for d in raw_dirs:
        print(f"  {d}")
    print()

    for group_name in raw_dirs:
        clean_name = group_name.replace("Raw_", "").replace("raw_", "")
        source_dir = os.path.join(INPUT_DIR, group_name)
        target_dir = os.path.join(OUTPUT_DIR, clean_name)

        # --- REKURSIV alle Dateien sammeln ---
        tasks = []
        for root, dirs, files in os.walk(source_dir):
            rel_dir = os.path.relpath(root, source_dir)
            if rel_dir == ".":
                rel_dir = ""

            out_dir = os.path.join(target_dir, rel_dir)
            for filename in files:
                source_path = os.path.join(root, filename)
                target_path = os.path.join(out_dir, filename)
                tasks.append((source_path, target_path, out_dir))

        if not tasks:
            print(f"--- {group_name}: no files found ---")
            continue

        print(f"--- {group_name} -> {clean_name}: {len(tasks)} files ---")

        ok = 0
        err = 0
        for source_path, target_path, out_dir in tqdm(tasks, desc=clean_name):
            try:
                os.makedirs(out_dir, exist_ok=True)

                with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
                    raw_content = f.read()

                cleaned_content = clean_asm(raw_content)

                if cleaned_content.strip():
                    with open(target_path, "w", encoding="utf-8") as f:
                        f.write(cleaned_content)
                    ok += 1
            except Exception as e:
                print(f"  Error processing {os.path.relpath(source_path, INPUT_DIR)}: {e}")
                err += 1

        print(f"  [OK] {ok} processed, {err} errors\n")

    print("Finished!")


if __name__ == "__main__":
    main()
