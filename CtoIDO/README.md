# MIPS Assembly Generator & Cleaner

> C source → IDO compiler → spimdisasm → ASM_Raw (merged) + ASM_Cleaned (cleaned)

## Overview

This tool converts C files into clean, comment-free MIPS assembly. It produces **two outputs per file**:

1. **ASM_Raw/** – The merged raw output from spimdisasm (all sections in one file: `.text`, `.data`, `.rodata`, `.bss`).
2. **ASM_Cleaned/** – The cleaned version with comments removed, normalised labels, and evaluated static bit operations.

## Directory Structure

```
.
├── pipeline.py             # Main script
├── IDO_compiler/           # IDO compiler toolchain (cc, etc.)
├── Input_C/                # Input: .c files
├── Input_C_headers/        # Input: corresponding .h files
├── ASM_Raw/                # Output: merged raw ASM
├── ASM_Cleaned/            # Output: cleaned MIPS ASM
├── Failed/                 # Failed .c/.h pairs
└── .tmp_pipeline/          # Temporary files (deleted automatically)
```

## Requirements

- **gcc** – for the C preprocessor
- **IDO Compiler** – place the IDO toolchain in the `IDO_compiler/` folder
- **spimdisasm** – `pip install spimdisasm`
- **tqdm** – `pip install tqdm`

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Place the IDO compiler
# Copy your IDO toolchain to ./IDO_compiler/
# Make sure ./IDO_compiler/cc exists and is executable.
```

## Usage

1. Place your `.c` files in `Input_C/`.
2. Place the corresponding `.h` files in `Input_C_headers/`.
3. Run the script:

```bash
python3 pipeline.py
```

4. Outputs are written to:
   - `ASM_Raw/` – Merged raw ASM with all sections
   - `ASM_Cleaned/` – Cleaned, compact version
5. Failed files are moved to `Failed/` (including the error message).

## What the Script Does

1. **Ghost-call firewall** – Checks for forbidden system calls (SDL, POSIX, etc.) and filters those files immediately.
2. **Header sanitisation** – Cleans up relative includes and invalid `extern` declarations.
3. **Preprocessor (gcc)** – Expands macros and includes.
4. **Compilation (IDO)** – Produces MIPS ELF object files with authentic N64 optimisations (`-O2 -mips2 -G0`).
5. **Disassembly (spimdisasm)** – Converts the object file into readable assembly.
6. **Section merge** – Combines `.text`, `.data`, `.rodata`, and `.bss` into a single `.s` file (→ `ASM_Raw/`).
7. **Cleanup** –
   - Removes address/hex comments
   - Normalises labels
   - Evaluates static bit operations (`>> 16`, `& 0xFFFF`) directly
   - Removes redundant assembler directives
   (→ `ASM_Cleaned/`)

## Configuration

The main settings are found at the top of `pipeline.py`:

| Variable | Description |
|----------|-------------|
| `BASE_DIR` | Base directory (defaults to the script directory) |
| `IDO_DIR` / `IDO_CC` | Path to the IDO compiler |
| `INCLUDE_DIRS` | Additional include paths for the preprocessor |
| `CPP_FLAGS` / `IDO_FLAGS` | Compiler flags |
| `FORBIDDEN_PATTERNS` | Regex patterns for the ghost-call firewall |

## Troubleshooting

**"IDO compiler not found"**
→ Make sure `IDO_compiler/cc` exists and is executable (`chmod +x IDO_compiler/cc`).

**"GCC error"**
→ Check that all required headers are present in `Input_C_headers/` or in `INCLUDE_DIRS`.

**Files in `Failed/`**
→ The original files were moved there. Check the error message, fix the code, and move the files back to `Input_C/` and `Input_C_headers/` respectively.

## Licence

This project is a tool for processing N64 assembly. Use at your own risk.
