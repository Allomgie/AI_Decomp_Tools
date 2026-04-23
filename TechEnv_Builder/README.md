# MIPS Semantic Expert Extractor

A Python tool that performs deep semantic analysis on MIPS assembly files (IDO compiler output) and extracts structured metadata into JSON format. It reconstructs function signatures, stack frames, memory access patterns, call graphs, and argument/return value inference using paired C header files.

## Overview

This tool bridges the gap between raw MIPS assembly and high-level semantic understanding. It is particularly useful for:
- **Reverse Engineering**: Decompiling legacy MIPS binaries (e.g., Nintendo 64, PlayStation 2).
- **Compiler Analysis**: Understanding IDO compiler patterns and calling conventions.
- **Dataset Generation**: Creating structured training data for machine learning models targeting assembly-to-source translation.

## Features

- **Header Parsing**: Uses `pycparser` to parse C headers and extract struct layouts, function prototypes, global symbols, and enums.
- **Register Tracking**: Tracks register origins (symbol addresses, stack frame pointers, immediate values) across the entire function.
- **Stack Frame Reconstruction**: Identifies saved registers and local variable blocks from stack pointer arithmetic.
- **Memory Access Analysis**: Maps memory accesses to struct fields or global variables when type information is available.
- **Call Graph Extraction**: Detects direct calls (`jal`), indirect calls (`jalr`), and tail calls (`j`), including argument count inference.
- **Argument & Return Inference**: Determines function arguments from register usage patterns (including float arguments via `f12`/`f14`) and return values via `$v0` writes.
- **Blind-Save Filtering**: Distinguishes between genuine incoming arguments and compiler-generated stack saves that are never read back.
- **Branch Analysis**: Identifies backward branches (loops) and likely branches.
- **Multiprocessing**: Processes large batches of files in parallel.

## Project Structure

```
.
├── techenv_builder.py   # Main script
├── requirements.txt     # Python dependencies
├── Input_ASM/           # Place your .s assembly files here
├── Input_Header/        # Place corresponding .h header files here
└── Output_JSON/         # Generated JSON output
```

## Requirements

- Python 3.8+
- See `requirements.txt` for package dependencies.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

1. Place your MIPS assembly files (`.s`) into the `Input_ASM/` directory.
2. Place the corresponding C header files (`.h`) into the `Input_Header/` directory.
   - **Important**: The header file must have the same basename as the assembly file.
   - Example: `Input_ASM/game_logic.s` pairs with `Input_Header/game_logic.h`
3. Run the extractor:

```bash
python techenv_builder.py
```

4. Find the resulting JSON files in `Output_JSON/`.

## Output Format

The tool generates one JSON file per input pair. The exact schema is documented in [FORMAT.md](FORMAT.md).
A quick overview:

- **env**: Struct layouts, global symbols, function signatures, enums
- **funcs[]**: Per-function analysis (stack, memory, calls, branches, args, return type)

## Technical Highlights

- **Performance Optimized**: All regex patterns are compiled at module level to avoid recompilation on every assembly line.
- **Read-Modify-Write Detection**: Correctly handles MIPS idioms like `addiu $a0, $a0, 8` as reads of incoming arguments rather than pure overwrites.
- **IDO Compiler Awareness**: Specifically tailored for the IDO compiler's output patterns, including tail-call optimization and blind argument saves.

## License

This project is provided as-is for demonstration and research purposes.
