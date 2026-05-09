# -*- coding: utf-8 -*-
"""
m2c_argc.py  -  Headless m2c argc + return-type extractor

Uses m2c's CFG + liveness analysis to determine:
  - The true number of incoming arguments (argc) for each function
  - Whether the function returns a value (non-void) or not

Usage from stage2_semantic_expert.py:
    from m2c_argc import get_argc_ret_map, get_argc_map, M2C_AVAILABLE
    result = get_argc_ret_map(s_path)
    # result: dict[func_name -> {"argc": int, "ret": bool}]
    # argc: capped at 4, ret: True = non-void, False = void

    # Legacy API (unchanged):
    argc_map = get_argc_map(s_path)
    # argc_map: dict[func_name -> int]

Requirements:
    - /home/lukas/ai_database/m2c must be on sys.path
    - Target: mips-ido-c  (IDO compiler, MIPS O32)
"""

from __future__ import annotations
import sys
import os
import io
import re
from pathlib import Path
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# Ensure m2c is importable
# ---------------------------------------------------------------------------
M2C_DIR = "/home/lukas/ai_database/m2c"
if M2C_DIR not in sys.path:
    sys.path.insert(0, M2C_DIR)

try:
    from m2c.main import parse_flags, run
    from m2c.asm_file import parse_file, AsmData
    from m2c.arch_mips import MipsArch
    from m2c.flow_graph import build_flowgraph
    from m2c.options import Options, Target
    from m2c.translate import (
        translate_to_ast,
        GlobalInfo,
        narrow_func_call_outputs,
        FunctionInfo,
    )
    from m2c.c_types import build_typemap
    from m2c.types import TypePool, Type
    M2C_AVAILABLE = True
except ImportError as _e:
    M2C_AVAILABLE = False
    _M2C_IMPORT_ERROR = str(_e)


def _make_options(s_path: str, c_context: Optional[str] = None) -> "Options":
    """Build a minimal Options object for a single .s file."""
    flags = [
        s_path,
        "--target", "mips-ido-c",
        "--passes", "2",       # two passes for better type resolution
        "--globals", "none",   # we don't need global decl output
        "--no-unk-inference",  # faster, we don't need type inference
    ]
    if c_context and os.path.exists(c_context):
        flags += ["--context", c_context]
    return parse_flags(flags)


def _is_void_return(info: "FunctionInfo") -> bool:
    """
    Determine if a function returns void based on m2c's analysis.
    
    m2c's FunctionInfo contains the deduced return type. If m2c
    determines that $v0 is not live-on-exit (no caller uses the
    return value), the return type will be void.
    
    We check multiple signals:
    1. info.return_type: the deduced C return type
    2. The generated AST: if it contains return statements with values
    3. stack_info.return_addr_save_offset: presence indicates non-leaf
    """
    try:
        # Primary: check the function's return type from m2c's type system
        ret_type = info.return_type
        if ret_type is not None:
            ret_str = str(ret_type)
            if ret_str == "void" or ret_str == "None":
                return True
            if ret_str != "" and ret_str != "void":
                return False
    except (AttributeError, TypeError):
        pass
    
    try:
        # Fallback: check if the function signature includes void return
        # by looking at the generated C output
        fn_type = getattr(info, 'fn_type', None)
        if fn_type is not None:
            ret = getattr(fn_type, 'return_type', None)
            if ret is not None:
                ret_str = str(ret)
                if "void" in ret_str.lower():
                    return True
                return False
    except (AttributeError, TypeError):
        pass
    
    # If we can't determine, return None to signal uncertainty
    return None


def get_argc_ret_map(
    s_path: str,
    c_context: Optional[str] = None,
    debug: bool = False,
) -> Optional[Dict[str, dict]]:
    """
    Run m2c's CFG + liveness analysis on a .s file and return a dict mapping
    function name -> {"argc": int, "ret": bool}
    
    argc: capped at 4 (O32 register args)
    ret: True = non-void, False = void, None = uncertain
    
    Returns None if m2c is not available or if parsing fails entirely.
    """
    if not M2C_AVAILABLE:
        return None

    try:
        return _run_m2c_analysis(s_path, c_context, debug=debug,
                                 include_ret=True)
    except Exception as e:
        if debug:
            import traceback
            traceback.print_exc()
        return None


def get_argc_map(
    s_path: str,
    c_context: Optional[str] = None,
    debug: bool = False,
) -> Optional[Dict[str, int]]:
    """
    Legacy API: returns dict[func_name -> argc] only.
    """
    if not M2C_AVAILABLE:
        return None

    try:
        full = _run_m2c_analysis(s_path, c_context, debug=debug,
                                  include_ret=False)
        if full is None:
            return None
        return {k: v if isinstance(v, int) else v["argc"]
                for k, v in full.items()}
    except Exception as e:
        if debug:
            import traceback
            traceback.print_exc()
        return None


def _preprocess_asm(s_path: str) -> str:
    """
    Return a preprocessed version of the .s file that m2c can parse.
    """
    import tempfile
    strip_patterns = [
        re.compile(r'^\s*\.include\s+"[^"]*"', re.MULTILINE),
        re.compile(r'^\s*nonmatching\s+\w+.*$', re.MULTILINE),
        re.compile(r'^\s*endnonmatching\b.*$', re.MULTILINE),
        re.compile(r'^\s*/\*\s*assembler directives\s*\*/', re.MULTILINE),
        re.compile(r'^\s*/\*\s*Generated by.*\*/', re.MULTILINE),
    ]

    with open(s_path, "r", encoding="utf-8-sig", errors="replace") as f:
        content = f.read()

    for pat in strip_patterns:
        content = pat.sub('', content)

    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.s', delete=False,
        dir=os.path.dirname(s_path), encoding='utf-8'
    )
    tmp.write(content)
    tmp.close()
    return tmp.name


def _run_m2c_analysis(
    s_path: str,
    c_context: Optional[str],
    debug: bool = False,
    include_ret: bool = True,
) -> Dict[str, any]:
    """Internal: run full m2c analysis and extract argc (+ ret) per function."""
    tmp_path = _preprocess_asm(s_path)
    try:
        return _run_m2c_on_file(tmp_path, c_context, debug, include_ret)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _run_m2c_on_file(
    s_path: str,
    c_context: Optional[str],
    debug: bool = False,
    include_ret: bool = True,
) -> Dict[str, any]:
    """Run m2c on a (preprocessed) .s file."""
    arch = MipsArch()
    options = _make_options(s_path, c_context)

    with open(s_path, "r", encoding="utf-8-sig", errors="replace") as f:
        asm_file = parse_file(f, arch, options)

    asm_data = AsmData()
    asm_file.asm_data.merge_into(asm_data)
    all_functions = {fn.name: fn for fn in asm_file.functions}

    if debug:
        print(f"  [m2c debug] found {len(all_functions)} functions")

    if not all_functions:
        return {}

    typemap = build_typemap(options.c_contexts, arch, use_cache=False)
    typepool = TypePool(
        unknown_field_prefix="unk",
        unk_inference=False,
        union_field_overrides={},
    )
    global_info = GlobalInfo(
        asm_data,
        arch,
        options.target,
        set(all_functions.keys()),
        typemap,
        typepool,
        deterministic_vars=False,
        stack_spill_detection=options.stack_spill_detection,
    )

    flow_graphs = {}
    for name, function in all_functions.items():
        try:
            narrow_func_call_outputs(function, global_info)
            fg = build_flowgraph(
                function,
                global_info.asm_data,
                arch,
                fragment=False,
                print_warnings=False,
                debug_patterns=False,
            )
            flow_graphs[name] = fg
        except Exception as e:
            if debug:
                print(f"  [m2c debug] CFG failed for {name}: {e}")

    for _ in range(options.passes - 1):
        for name, function in all_functions.items():
            fg = flow_graphs.get(name)
            if fg is None:
                continue
            try:
                fg.reset_block_info()
                translate_to_ast(function, fg, options, global_info)
            except Exception:
                pass
        try:
            typepool.prune_structs()
        except Exception:
            pass

    # Final pass: extract argc and ret
    result: Dict[str, any] = {}
    ARG_REG_ORDER = {"a0": 0, "a1": 1, "a2": 2, "a3": 3}

    for name, function in all_functions.items():
        fg = flow_graphs.get(name)
        if fg is None:
            continue
        try:
            fg.reset_block_info()
            info: FunctionInfo = translate_to_ast(function, fg, options,
                                                   global_info)

            # ---- argc ----
            found = set()
            for a in info.stack_info.arguments:
                if a.loc.reg is not None:
                    rname = a.loc.reg.register_name
                    if rname in ARG_REG_ORDER:
                        found.add(ARG_REG_ORDER[rname])

            if found:
                argc = max(found) + 1
            else:
                argc = 0
            argc = min(argc, 4)

            # ---- ret ----
            if include_ret:
                is_void = _is_void_return(info)
                result[name] = {"argc": argc, "ret": not is_void if is_void is not None else None}
            else:
                result[name] = argc

            if debug:
                if include_ret:
                    print(f"  [m2c debug] {name}: argc={argc}, ret={result[name]['ret']}")
                else:
                    print(f"  [m2c debug] {name}: argc={argc}")

        except Exception as e:
            if debug:
                import traceback
                traceback.print_exc()

    return result


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not M2C_AVAILABLE:
        print(f"m2c not available: {_M2C_IMPORT_ERROR}")
        sys.exit(1)

    if len(sys.argv) < 2:
        print("Usage: python3 m2c_argc.py <asm_file.s> [context.c] [--debug]")
        sys.exit(1)

    s = sys.argv[1]
    ctx = None
    debug = False
    for arg in sys.argv[2:]:
        if arg == "--debug":
            debug = True
        elif not arg.startswith("--"):
            ctx = arg

    result = get_argc_ret_map(s, ctx, debug=debug)
    if result is None:
        print("Failed")
    else:
        for fname, data in sorted(result.items()):
            print(f"  {fname}: argc={data['argc']}, ret={'non-void' if data['ret'] else 'void' if data['ret'] is not None else '?'}")