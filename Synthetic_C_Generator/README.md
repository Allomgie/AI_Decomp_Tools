# C Code Generators for IDO 5.3 / MIPS

This directory contains the fuzzing engines that supply input for the [Dead-Code Reducer Pipeline](../README.md). The goal is the controlled generation of C code that is guaranteed to compile to valid MIPS assembly with the IDO 5.3 compiler (Nintendo 64 / SGI MIPS).

---

## Overview

| Generator | Engine | Notable feature | Output |
|-----------|--------|-----------------|--------|
| `gen_csmith_split2.py` | Csmith 2.3.0 | Header/C split, sandbox isolation | `dataset/C/` + `dataset/header/` + `dataset/ASM/` |
| `gen_csmith_switchCase.py` | Csmith 2.3.0 + pycparser | AST mutation: `for`→`do-while`, switch-case injection | `dataset/C/` + `dataset/header/` + `dataset/ASM/` |
| `gen_YARPGen_split.py` | YARPGen (patched) | Syntax firewall, dual output (init.h + func.c) | `dataset/C/` + `dataset/header/` + `dataset/ASM/` |

---

## Shared Architecture

All three generators share a common design:

1. **Generation** – Csmith/YARPGen produces raw C code from a seed
2. **Sanitising** – Type replacements (`uint32_t` → `u32`), removal of keywords (`static`, `volatile`), filtering of non-MIPS-compatible constructs
3. **Splitting** – Separation into header (structs, external globals) and C file (implementation)
4. **Preprocessing** – `gcc -E` with IDO-compatible flags (`-D_LANGUAGE_C`, `-D_MIPS_SZLONG=32`)
5. **Compilation** – IDO 5.3 (`cc -S -O2 -mips2 -G0`) produces MIPS assembly
6. **Cleanup** – Isolated sandbox (`tmp_<seed>/`) is destroyed after each run

---

## Individual Generators

### `gen_csmith_split2.py` – Base Generator

The standard generator. Produces simple C functions with controlled complexity.

**Csmith parameter tuning for MIPS:**
```python
--max-funcs 1           # One function per file only
--no-longlong           # No 64-bit integers (IDO 5.3 limitation)
--no-math64             # No 64-bit arithmetic
--no-safe-math          # Allows overflow/undefined behaviour
--no-arrays             # No arrays (simplifies splitting)
--max-block-depth 2-4   # Controlled nesting depth
--max-expr-complexity 2-5
```

**Usage:**
```bash
python gen_csmith_split2.py
# Generates 40,000 samples automatically (configurable in run_production())
```

---

### `gen_csmith_switchCase.py` – AST Mutator

Extends the base generator with **pycparser-based AST transformations**. After Csmith generation, the code is parsed, mutated, and regenerated.

**Transformations:**
- **For→Do-While:** `for(init; cond; next){ body }` becomes:
  ```c
  init;
  if (cond) {
      do {
          body;
          next;
      } while (cond);
  }
  ```
- **Switch-case injection:** Blocks of 3–6 statements are converted into switch statements using `rand_state % n` as the dispatcher

**Why?** These patterns appear frequently in decompiled N64 code. The model needs to learn to recognise and simplify them.

**Usage:**
```bash
python gen_csmith_switchCase.py
# Generates 60,000 samples (slower than base due to pycparser)
```

---

### `gen_YARPGen_split.py` – YARPGen Integration

Integrates YARPGen as a second fuzzing engine. YARPGen produces more complex constructs (multiple functions, pointer arithmetic, nested structs) that Csmith does not cover.

**Syntax firewall:**
Since YARPGen was developed for modern x86 compilers, the generator actively filters incompatible constructs:

```python
# Blocked patterns (PC/Linux-specific)
SDL_, Py, linux, posix, WEXITSTATUS, setpgid, signal, _exit, getpid

# Syntax guard
return x  # must end with ; otherwise aborted
```

**Dual output:**
YARPGen produces two files:
- `init.h` – Global variables & constants
- `func.c` – Function logic

The generator automatically splits these into the header/C schema used by the pipeline.

> **Note:** Requires a patched YARPGen binary for IDO 5.3 compatibility. The upstream binary (https://github.com/intel/yarpgen) generates constructs that IDO 5.3 cannot process (e.g. certain attributes, modern C features). The code in this file shows the integration architecture — the binary itself is not included in the repository.

**Usage:**
```bash
python gen_YARPGen_split.py
# Generates 60,000 samples (patched YARPGen binary required)
```

---

## Configuration

Paths must be adjusted to your local environment:

```python
# In all three files:
BASE_DIR      = "/home/user/deadCodeRemover"
PROJECT_ROOT  = os.path.join(BASE_DIR, "IDO_compiler")  # <-- adjust
IDO_DIR       = os.path.join(PROJECT_ROOT, "tools", "ido")
CSMITH_BIN    = os.path.join(BASE_DIR, "csmith_install/bin/csmith")
YARPGEN_BIN   = "/path/to/yarpgen"  # <-- adjust
```

**Key paths:**
- `IDO_DIR` – Path to the IDO 5.3 compiler (`cc`)
- `CSMITH_BIN` – Csmith executable
- `YARPGEN_BIN` – Patched YARPGen binary
- `INCLUDE_DIR_*` – Project headers (ultralib, PR, etc.)

---

## Output Structure

```
n64_dataset/
├── C/               # Generated .c files
│   ├── csmith_sample_12345.c
│   └── yarp_sample_678.c
├── header/          # Corresponding .h files
│   ├── csmith_sample_12345.h
│   └── yarp_sample_678.h
└── ASM/             # Compiled MIPS assembly (.s)
    ├── csmith_sample_12345.s
    └── yarp_sample_678.s
```

---

## Performance

| Generator | Throughput | Limiting factor |
|-----------|------------|-----------------|
| `gen_csmith_split2.py` | ~500–1000 samples/s | IDO compilation |
| `gen_csmith_switchCase.py` | ~200–400 samples/s | pycparser AST mutation |
| `gen_YARPGen_split.py` | ~50–100 samples/s | YARPGen startup + syntax check |

All generators use **multiprocessing** (`multiprocessing.Pool`) with `cpu_count()` workers.

---

## Troubleshooting

**IDO cannot find headers:**
- Check `INCLUDE_DIR_1` through `INCLUDE_DIR_4` and `CSMITH_INC`
- Headers must be visible to gcc preprocessing via `-I`

**YARPGen segfaults:**
- Normal behaviour for ~30% of seeds. The generator catches this and tries the next seed.
- If >90% fail: the YARPGen binary is not correctly patched.

**Csmith produces empty files:**
- Check that `csmith` is reachable in PATH
- `--max-funcs 1` can produce empty output for certain seeds — the generator skips these automatically

---

## Licence

See [../LICENSE](../LICENSE). The generators are part of the Dead-Code Reducer project and are released under the MIT licence.

**Third-party tool notices:**
- Csmith is licenced under BSD (https://github.com/csmith-project/csmith)
- YARPGen is licenced under Apache 2.0 (https://github.com/intel/yarpgen)
- IDO 5.3 is proprietary software from SGI/Nintendo
