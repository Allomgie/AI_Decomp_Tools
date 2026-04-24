# AI_Decomp_Tools

This repository bundles pipeline scripts and tools for semantic analysis, code generation, and MIPS assembly processing, primarily designed for N64 decompilation workflows.

The repository consists of three separate but complementary tools:

## 1. CtoIDO
**MIPS Assembly Generator & Cleaner**

This tool converts C files into clean, comment-free MIPS assembly. It is designed to process code faithfully using the IDO compiler.

- **Pipeline:** Code passes through a chain of header sanitisation, preprocessor (gcc), compilation (IDO), and disassembly (spimdisasm).
- **Outputs:** Two artefacts are generated per C file:
    - `ASM_Raw`: A merged output of all sections (`.text`, `.data`, `.rodata`, `.bss`).
    - `ASM_Cleaned`: A cleaned version without comments, with normalised labels and evaluated static bit operations.
- *See the [CtoIDO subfolder](CtoIDO/README.md) for details.*

## 2. Synthetic_C_Generator
**Fuzzing engines for IDO 5.3 / MIPS**

This directory contains generators that automatically produce C code guaranteed to compile to valid MIPS assembly with the IDO 5.3 compiler. The tools serve as data suppliers for training machine learning models (e.g. a dead-code reducer pipeline).

- **Engines:**
    - Base Csmith generator for simple, controlled functions.
    - AST mutator (via pycparser) for targeted injection of typical decompilation patterns such as specific switch-cases and do-while loops.
    - Patched YARPGen integration for complex constructs and pointer arithmetic.
- *See the [Synthetic_C_Generator subfolder](Synthetic_C_Generator/README.md) for details.*

## 3. TechEnv_Builder (Work in Progress)
**MIPS Semantic Expert Extractor**

An analysis tool under development that bridges the gap between raw MIPS assembly and high-level C semantics.

- **Function:** Analyses MIPS assembly files (IDO output) and links them with C header definitions to extract deep semantic metadata.
- **Features:** Reconstructs register tracking, stack frame layouts, variable typing, and call graphs. Detects specific compiler patterns such as tail calls and blind saves.
- **Output format:** Generates a dense, token-efficient JSON per file (`Semantic Expert IR - v10`), structured into a static type database (`env`) and per-function analyses (`funcs[]`).
- *See the [TechEnv_Builder subfolder](TechEnv_Builder/README.md) for details.*
