# MIPS Semantic Expert Extractor (TechEnv Builder)

> ASM file + Header file + `global_symbols_<group>.jsonl` → structured JSON metadata per function

A Python tool that performs deep semantic analysis on MIPS assembly files (IDO 5.3 compiler output) and extracts structured metadata into JSON format. It reconstructs function signatures, stack frames, memory access patterns, call graphs, and argument/return value inference — using paired C header files and section metadata from a global symbols map.

---

## Overview

This tool bridges the gap between raw MIPS assembly and high-level semantic understanding. It is particularly useful for:

- **Dataset generation:** Creating structured training data for LLM fine-tuning on assembly-to-source translation (the primary use case here).
- **Reverse engineering:** Decompiling legacy MIPS binaries (e.g. Nintendo 64).
- **Compiler analysis:** Understanding IDO 5.3 compiler patterns and calling conventions.

### Validation results (v16, 258k files)

| Metric | Result |
|--------|--------|
| Parse success | 98.3% |
| Function recall | 100.0% |
| Argument accuracy (high confidence) | 96.5% |
| Return type accuracy (high confidence) | 93.9% |
| Call graph recall (F1) | 97.4% |
| Global access recall (F1) | 96.0% |

---

## Inputs

Each processed file requires three inputs:

| Input | Description |
|-------|-------------|
| `<name>.s` | MIPS assembly file (IDO compiler output, cleaned by CtoIDO) |
| `<name>.h` | Corresponding C header file |
| `global_symbols_<group>.jsonl` | Section map for the group (one JSON object per line) |

### global_symbols JSONL format

Each line describes the section membership of one assembly file:

```json
{
  "file": "func_global_asm_806D2C54_v0.s",
  "function": "func_global_asm_806D2C54_v0",
  "sections": {
    ".rodata": [],
    ".data": [],
    ".bss": [],
    ".text": ["func_global_asm_806D2B50"]
  }
}
```

The `sections` field is critical: it tells the extractor which symbols are data globals (`.data`, `.bss`, `.sdata`, `.sbss`) versus code labels (`.text`, `.rodata`). This drives the `_is_gvar` filter and is the primary mechanism for suppressing false-positive global reads.

---

## Output

One JSON file per input pair, written to `JSON_Expert_<group>/`. Structure:

```json
{
  "file": "example.s",
  "env": {
    "s": { "StructName": { "0x0": "s32", "0x4": "u32 *" } },
    "sym": { "gSomeGlobal": "u32" },
    "ext": { "someFunc": "(s32, u32 *) -> void" },
    "e": { "EnumName": { "0": "ENUM_A", "1": "ENUM_B" } }
  },
  "funcs": [
    {
      "n": "func_80123456",
      "sf": 32,
      "args": ["a0", "a1"],
      "argc_conf": "high",
      "ret": "v0",
      "ret_conf": "high",
      "calls": [{ "type": "direct", "name": "otherFunc", "argc": 2 }],
      "globals": { "gSomeGlobal": "r" },
      "stk": { "saved": { "ra": "0x1c" }, "locals": { "singles": 2 } },
      "mem": [],
      "br": [{ "op": "beq", "loop": true }],
      "flags": { "has_fpu": true }
    }
  ]
}
```

### Key output fields

| Field | Description |
|-------|-------------|
| `env.s` | Struct layouts with field offsets and types |
| `env.sym` | Global symbol names and types from the header |
| `env.ext` | External function signatures |
| `env.e` | Enum name→value mappings |
| `funcs[].n` | Function name |
| `funcs[].sf` | Stack frame size in bytes |
| `funcs[].args` | Inferred argument registers (a0–a3) |
| `funcs[].argc_conf` | Confidence: `high`, `medium`, or `low` |
| `funcs[].ret` | Return register (`v0`) or `void` |
| `funcs[].ret_conf` | Confidence: `high`, `medium`, or `low` |
| `funcs[].calls` | Direct and indirect call graph |
| `funcs[].globals` | Global variables accessed, with read/write mode |
| `funcs[].stk` | Stack frame layout (saved registers, local blocks) |
| `funcs[].mem` | Struct field accesses via pointer |
| `funcs[].br` | Branch instructions (with loop/likely annotations) |
| `funcs[].flags` | Optional flags: `has_fpu`, `has_div`, `has_mul`, `has_64bit` |

---

## Directory Structure

```
TechEnv_Builder/
├── techenv_builder.py       # Main script
├── m2c_argc.py              # Optional: m2c argc/ret oracle (if available)
├── requirements.txt
├── dataset/
│   ├── input_group/                    # C source files (.c)
│   ├── input_group_headers/            # Header files (.h)
│   ├── ASM_Raw_input_group/            # Assembly files (.s)
│   ├── global_symbols_input_group.jsonl
│   └── JSON_Expert_input_group/        # Output JSON files
└── README.md
```

---

## Requirements

```bash
pip install pycparser tqdm
```

The optional `m2c_argc` module (a lightweight wrapper around the m2c decompiler) improves argument count and return type accuracy when available. The tool falls back to heuristic-only mode if it is not found.

---

## Configuration

At the top of `techenv_builder.py`:

```python
BASE_DIR    = "/path/to/your/workspace"
DATASET_DIR = os.path.join(BASE_DIR, "dataset")

USE_M2C = True   # Set to False for heuristic-only mode (faster)

GROUPS = [
    "input_group",  # Replace with your actual group names
]
```

The script expects the following layout per group inside `DATASET_DIR`:

| Path | Content |
|------|---------|
| `<group>/` | C source files |
| `<group>_headers/` | Header files |
| `ASM_Raw_<group>/` | Assembly files |
| `global_symbols_<group>.jsonl` | Section map |
| `JSON_Expert_<group>/` | Output (created automatically) |

---

## Usage

```bash
python techenv_builder.py
```

The script processes all groups defined in `GROUPS` sequentially, using all available CPU cores per group via `multiprocessing.Pool`.

---

## Technical Highlights

**Section-aware global detection (`_is_gvar`)**
The primary mechanism for distinguishing data globals from code labels. Symbols listed in `.data`, `.bss`, `.sdata`, `.sbss`, `.comm`, or `.lcomm` in the JSONL map are accepted as globals regardless of their name. Symbols listed in `.text` or `.rodata` are rejected. This eliminates the vast majority of false positives from jump table entries and local labels.

**Stale pointer tracking**
Every writing ALU or load instruction invalidates the destination register in `reg_map` unless it is an explicit symbol load (`lui %hi`, `la`, `%got`, `%lo`, `%gp_rel`). This prevents stale pointer chains from incorrectly attributing later memory accesses to a symbol loaded several instructions earlier.

**Argument save/reload detection**
Distinguishes genuine incoming arguments from compiler-generated blind saves: a register is only counted as a true argument if it is reloaded from the stack and used after the save, or if it is tainted into a saved register and used post-call.

**Confidence scoring**
All argument count and return type inferences carry a confidence level (`high` / `medium` / `low`). High confidence is assigned when the header provides an explicit signature, or when argument saves are confirmed by reloads. Medium confidence applies to heuristic-only inferences.

**Optional m2c oracle**
When `m2c_argc.py` is present, the tool runs a lightweight CFG + liveness analysis via m2c to cross-check heuristic argc and ret inferences, upgrading or correcting them where the two sources disagree.

---

## Known Limitations

- **IDO 5.3 specific:** Tailored to IDO compiler output patterns (calling conventions, tail calls, blind saves). Not directly applicable to GCC or Clang output without modification.
- **argc capped at 4:** MIPS O32 calling convention passes at most 4 arguments in registers (a0–a3). Stack-passed arguments are not inferred.
- **Header dependency:** Global type information and external function signatures require a corresponding `.h` file. Without it, `env` will be empty and globals will be identified by name pattern only.
- **No full C99 support:** pycparser does not handle all C99 constructs (variable-length arrays, complex initialisers). Parse failures fall back gracefully to an empty env.

---

## Licence

This project is provided for research and dataset generation purposes.
