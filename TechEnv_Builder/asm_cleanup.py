# -*- coding: utf-8 -*-
"""
0_asm_cleanup_raw.py
====================
Cleans MIPS assembly files: removes comments, normalizes formatting,
and evaluates static bit operations (>> 16, & 0xFFFF) directly within instructions.

Reads from:  INPUT_DIR
Writes to:   OUTPUT_DIR
"""
import os
import re
from tqdm import tqdm

# --- CONFIGURATION ---
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR  = os.path.join(BASE_DIR, "input_raw_asm")
OUTPUT_DIR = os.path.join(BASE_DIR, "output_clean_asm")


def clean_asm(raw_asm):
    """
    Cleans MIPS assembly and evaluates static bit operations
    like >> 16 and & 0xFFFF directly within instructions.
    """
    cleaned = []
    lines = raw_asm.splitlines()

    # Pattern for instructions
    instr_pattern = re.compile(
        r"^\s*/\*\s*[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+([0-9A-Fa-f]{8})\s*\*/\s*(.*)$")
    # Pattern for data directives
    data_pattern = re.compile(
        r"^\s*/\*\s*[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s*\*/\s*(\.[a-z]+\s+.*)$")
    # Pattern for exclusive hex code lines
    hex_only_pattern = re.compile(
        r"^\s*/\*\s*([0-9A-Fa-f]+)\s*\*/\s*$")
    # Pattern for bit operations in operands
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

        # 1. Keep function and data entry points
        if stripped_line.startswith(("glabel", "dlabel", ".section")):
            cleaned.append(stripped_line)
            i += 1
            continue

        # 2. Normalize local jump labels
        if stripped_line.endswith(":"):
            cleaned.append(f"00000000 {stripped_line}")
            i += 1
            continue

        # 3. Keep endlabel
        if stripped_line.startswith("endlabel"):
            cleaned.append(stripped_line)
            i += 1
            continue

        # 4. Process instructions & evaluate bit-ops
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

        # 5. Merge data directives with hex codes
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

    # Automatically detect all group folders in the input directory
    raw_dirs = sorted([
        d for d in os.listdir(INPUT_DIR)
        if os.path.isdir(os.path.join(INPUT_DIR, d))
    ])

    if not raw_dirs:
        print(f"No subdirectories found in '{INPUT_DIR}'! Please organize your ASM files into subfolders.")
        return

    print(f"Found groups: {len(raw_dirs)}")
    for d in raw_dirs:
        print(f"  {d}")
    print()

    for group_name in raw_dirs:
        # e.g., ASM_Raw_Save_00_generated -> ASM_Save_00_generated
        clean_name = group_name.replace("Raw_", "").replace("raw_", "")

        source_dir = os.path.join(INPUT_DIR, group_name)
        target_dir = os.path.join(OUTPUT_DIR, clean_name)
        os.makedirs(target_dir, exist_ok=True)

        # Collect all files in this folder
        files = [f for f in os.listdir(source_dir)
                 if os.path.isfile(os.path.join(source_dir, f))]

        print(f"--- {group_name} -> {clean_name}: {len(files)} files ---")

        ok = 0
        err = 0
        for filename in tqdm(files, desc=clean_name):
            source_path = os.path.join(source_dir, filename)
            target_path = os.path.join(target_dir, filename)

            try:
                with open(source_path, "r", encoding="utf-8", errors="ignore") as f:
                    raw_content = f.read()

                cleaned_content = clean_asm(raw_content)

                if cleaned_content.strip():
                    with open(target_path, "w", encoding="utf-8") as f:
                        f.write(cleaned_content)
                    ok += 1
            except Exception as e:
                print(f"  Error processing {filename}: {e}")
                err += 1

        print(f"  [OK] {ok} processed, {err} errors\n")

    print("Finished!")


if __name__ == "__main__":
    main()