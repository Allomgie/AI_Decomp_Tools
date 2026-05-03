# -*- coding: utf-8 -*-
r"""
Semantic Expert Extractor  v16

Changes vs v15:
  - Universal stale pointer tracking: every writing ALU/load instruction
    invalidates the destination register in reg_map unless it is an explicit symbol load.
  - Specific exclusion of Csmith local statics (l_\d+) and compiler jump tables
    (CSWTCH_, L-labels) in _is_gvar to eliminate the high false-positive rate.
"""

import os
import re
import sys
import json
import multiprocessing
import contextlib
from tqdm import tqdm
from pycparser import c_parser, c_ast, c_generator

# ---------------------------------------------------------------------------
# 1. CONFIGURATION
# ---------------------------------------------------------------------------

BASE_DIR    = "/path/to/your/workspace"
DATASET_DIR = os.path.join(BASE_DIR, "dataset")

# Set to False to disable m2c and use heuristic-only mode (much faster).
USE_M2C = True

GROUPS = [
    "input_group",  # Replace with your actual group names
]

# ---------------------------------------------------------------------------
# m2c integration: optional argc oracle via CFG + liveness analysis
# ---------------------------------------------------------------------------
M2C_AVAILABLE = False
def get_argc_ret_map(*a, **kw):
    return None

for _m2c_candidate in [
    os.path.join(BASE_DIR, "m2c"),
    os.path.join(BASE_DIR),
    os.path.dirname(os.path.abspath(__file__)),
]:
    if _m2c_candidate not in sys.path:
        sys.path.insert(0, _m2c_candidate)
    try:
        from m2c_argc import get_argc_ret_map, M2C_AVAILABLE  # type: ignore[assignment]
        break
    except ImportError:
        continue

OP_TO_TYPE = {
    'lb':   's8',  'lbu': 'u8',  'sb':   's8',
    'lh':  's16',  'lhu': 'u16', 'sh':  's16',
    'lw':  's32',  'sw':  's32',
    'lwc1':'f32',  'swc1':'f32',
    'ld':  's64',  'sd':  's64',
    'ldc1':'f64',  'sdc1':'f64',
}

SAVED_REGS = {'ra', 's0', 's1', 's2', 's3', 's4', 's5', 's6', 's7', 's8', 'gp'}

TYPEDEF_PREAMBLE = (
    "typedef signed char s8; typedef unsigned char u8; "
    "typedef signed short s16; typedef unsigned short u16; "
    "typedef signed int s32; typedef unsigned int u32; "
    "typedef signed long long s64; typedef unsigned long long u64; "
    "typedef float f32; typedef double f64; "
    "typedef unsigned int size_t; typedef unsigned int uint; "
    "typedef int bool; "
)

# ---------------------------------------------------------------------------
# 2. C / HEADER ANALYSIS
# ---------------------------------------------------------------------------

def _strip_declname(node):
    if node is None:
        return
    if hasattr(node, 'declname'):
        node.declname = None
    if hasattr(node, 'type') and node.type is not None:
        _strip_declname(node.type)


def _type_size(type_str, struct_sizes):
    t = type_str.strip()
    if '*' in t or '[' in t:
        return 4
    if any(x in t for x in ('s64', 'u64', 'long long', 'f64', 'double')):
        return 8
    if any(x in t for x in ('s32', 'u32', 'int', 'long', 'float', 'f32')):
        return 4
    if any(x in t for x in ('s16', 'u16', 'short')):
        return 2
    if any(x in t for x in ('s8', 'u8', 'char')):
        return 1
    m = re.search(r'struct\s+(\w+)', t)
    if m:
        return struct_sizes.get(m.group(1), 4)
    return 4


def _align(offset, size):
    align = min(size, 4)
    return (offset + align - 1) & ~(align - 1)


def _safe_visit(gen, node):
    if node is None:
        return ""
    try:
        result = gen.visit(node)
        return result if result is not None else ""
    except Exception:
        return ""


def _extract_enum(en_node, raw_enums):
    name = en_node.name
    if not name or not en_node.values:
        return
    mapping = {}
    cur_val = 0
    for enumerator in en_node.values.enumerators:
        if enumerator.value is not None:
            try:
                from pycparser import c_ast as _ca
                if isinstance(enumerator.value, _ca.Constant):
                    v = int(enumerator.value.value, 0)
                elif (isinstance(enumerator.value, _ca.UnaryOp)
                        and enumerator.value.op == '-'
                        and isinstance(enumerator.value.expr, _ca.Constant)):
                    v = -int(enumerator.value.expr.value, 0)
                else:
                    v = cur_val
            except (ValueError, TypeError):
                v = cur_val
            cur_val = v
        mapping[cur_val] = enumerator.name
        cur_val += 1
    raw_enums[name] = mapping


def parse_env_expert(c_path, h_dir):
    s_map, sym_map, ext_map, enum_map = {}, {}, {}, {}

    incs = []
    if os.path.exists(c_path):
        with open(c_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'\s*#include\s+"([^"]+)"', line)
                if m:
                    incs.append(m.group(1))
    base_h = os.path.splitext(os.path.basename(c_path))[0] + ".h"
    incs.append(base_h)

    header_content = ""
    for h in set(incs):
        p = os.path.join(h_dir, os.path.basename(h))
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                cleaned = re.sub(r'^\s*#.*$', '', f.read(), flags=re.MULTILINE)
                header_content += cleaned + "\n"

    if not header_content.strip():
        return s_map, sym_map, ext_map, enum_map

    parser = c_parser.CParser()
    gen    = c_generator.CGenerator()

    ast = None
    for preamble in (TYPEDEF_PREAMBLE,
                     TYPEDEF_PREAMBLE.replace("typedef unsigned int size_t; ", "")):
        try:
            ast = parser.parse(preamble + header_content)
            break
        except Exception:
            ast = None

    if ast is None:
        return s_map, sym_map, ext_map, enum_map

    struct_sizes = {}

    def _compute_struct_size(st_node):
        if not st_node.decls:
            return 0
        offset = 0
        for f in st_node.decls:
            if f.type is not None:
                _strip_declname(f.type)
            t_str = _safe_visit(gen, f.type).strip()
            sz = _type_size(t_str, struct_sizes)
            offset = _align(offset, sz)
            offset += sz
        return offset

    for node in ast.ext:
        if isinstance(node, c_ast.Decl) and isinstance(node.type, c_ast.Struct):
            st = node.type
            if st.name and st.decls:
                struct_sizes[st.name] = _compute_struct_size(st)

    BUILTIN_NAMES = {
        's8','u8','s16','u16','s32','u32','s64','u64','f32','f64',
        'size_t','uint','bool',
    }

    raw_enums = {}

    for node in ast.ext:
        if isinstance(node, c_ast.Decl) and isinstance(node.type, c_ast.Enum):
            en = node.type
            if en.name and en.values:
                _extract_enum(en, raw_enums)
        if (isinstance(node, c_ast.Decl)
                and isinstance(node.type, c_ast.TypeDecl)
                and isinstance(node.type.type, c_ast.Enum)):
            en = node.type.type
            if en.name and en.values:
                _extract_enum(en, raw_enums)

    for ename, vals in raw_enums.items():
        if re.search(r'\benum\s+' + re.escape(ename) + r'\b', header_content):
            enum_map[ename] = vals

    for node in ast.ext:
        if not isinstance(node, c_ast.Decl):
            continue

        if isinstance(node.type, c_ast.Struct):
            st = node.type
            if not st.decls:
                continue
            st_name = st.name if st.name else "unk"
            s_map[st_name] = {}
            offset = 0
            for f in st.decls:
                if f.type is not None:
                    _strip_declname(f.type)
                t_str = _safe_visit(gen, f.type).strip()
                sz = _type_size(t_str, struct_sizes)
                offset = _align(offset, sz)
                s_map[st_name][hex(offset)] = t_str
                offset += sz
            continue

        if not node.name or node.name in BUILTIN_NAMES:
            continue

        if isinstance(node.type, c_ast.FuncDecl):
            fd = node.type
            if fd.type is not None:
                _strip_declname(fd.type)
            ret = _safe_visit(gen, fd.type).strip()
            params = []
            if fd.args:
                for p in fd.args.params:
                    if isinstance(p, c_ast.EllipsisParam):
                        params.append("...")
                    else:
                        if p.type is not None:
                            _strip_declname(p.type)
                        params.append(_safe_visit(gen, p.type).strip())
            params_str = ", ".join(params) if params else "void"
            ext_map[node.name] = f"({params_str}) -> {ret}"
            continue

        if 'typedef' not in (node.storage or []):
            if re.match(r'^sp[0-9A-Fa-f]+$', node.name):
                continue
            _strip_declname(node.type)
            sym_map[node.name] = _safe_visit(gen, node.type).strip()

    return s_map, sym_map, ext_map, enum_map


# ---------------------------------------------------------------------------
# 3. ASM SEMANTIC ANALYSIS
# ---------------------------------------------------------------------------

_RE_GLABEL        = re.compile(r'^glabel\s+([a-zA-Z0-9_]+)')
_RE_ENDLABEL      = re.compile(r'^(?:endlabel|/\*\s*end function)')
_RE_HEX_PFX       = re.compile(r'^[0-9a-fA-F]{8}\s+')
_RE_COMMENT       = re.compile(r'/\*.*?\*/')

_RE_LUI           = re.compile(r'lui\s+\$(\w+),\s+%hi\((\w+)\)')
_RE_ADDIU_SYM     = re.compile(r'addiu\s+\$(\w+),\s+\$\w+,\s+%lo\((\w+)\)')
_RE_LA            = re.compile(r'la\s+\$(\w+),\s+(\w+)')
_RE_MOVE          = re.compile(r'(?:move|or)\s+\$(\w+),\s+\$(\w+)(?:,\s+\$zero)?$')
_RE_ADDU_ZERO     = re.compile(r'addu\s+\$(\w+),\s+\$(\w+),\s+\$zero')
_RE_LOAD_SP       = re.compile(r'\b(lw|ld)\s+\$(\w+),\s*(0x[\da-fA-F]+|-?\d+|\(0x[\da-fA-F]+\s*&\s*0xFFFF\))\(\$sp\)')
_RE_LOAD_NONSP    = re.compile(r'\blw\s+\$(\w+),\s*(0x[\da-fA-F]+|-?\d+|\(0x[\da-fA-F]+\s*&\s*0xFFFF\))\(\$(?!sp)(\w+)\)')

_RE_SF            = re.compile(r'addiu\s+\$sp,\s+\$sp,\s+-(0x[\da-fA-F]+|\d+)')
_RE_JAL           = re.compile(r'jal\s+(\w+)')
_RE_JALR          = re.compile(r'jalr\s+\$(\w+)')
_RE_TAIL_CALL     = re.compile(r'^j\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*$')

_RE_MEM = re.compile(
    r'\b(l[bBhHwWdD]u?|s[bBhHwWdD]|lwc1|swc1|ldc1|sdc1)\s+'
    r'\$(\w+),\s*(-?0x[\da-fA-F]+|-?\d+|\(0x[\da-fA-F]+\s*&\s*0xFFFF\))\(\$(\w+)\)'
)

_RE_BRANCH = re.compile(
    r'^(b(?:eq|ne|lez|gtz|ltz|gez|eqz|nez|ltzal|gezal)'
    r'|beql|bnel|blezl|bgtzl|bltzl|bgezl|b)\b'
    r'(?:[^,\n]+,\s*)?(\S+)\s*$'
)
_RE_LABEL_DEF     = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*):\s*$')
_RE_BRANCH_TARGET = re.compile(
    r'^(?:b(?:eq|ne|lez|gtz|ltz|gez|eqz|nez|ltzal|gezal)|beql|bnel|blezl|bgtzl|bltzl|bgezl|b)\b'
    r'.*\b([A-Za-z_][A-Za-z0-9_]*)\s*$'
)

_WRITE_OPS = (
    r'move|addu|addiu|subu|lw|lh|lb|lhu|lbu|lui|la|'
    r'or|ori|nor|and|andi|xor|xori|mflo|mfhi|mfc1|'
    r'sll|srl|sra|dsll|dsrl|dsra|slt|sltu|slti|sltiu'
)
_RE_ARG_WRITE = re.compile(
    r'\b(?:' + _WRITE_OPS + r')\s+\$(a0|a1|a2|a3)\b'
)
_RE_V0_WRITE = re.compile(
    r'\b(?:' + _WRITE_OPS + r')\s+\$v0\b'
)
_RE_ARG_SET = re.compile(
    r'\b(?:' + _WRITE_OPS + r')\s+\$(a[0-3])\b'
)
_RE_ADDIU_SP_ARG = re.compile(
    r'\baddiu\s+\$(a[0-3]),\s+\$sp,\s+'
)

# Fuer das rigorose Stale-Pointer Clearing (alle schreibenden Operationen)
_ALU_OPS = (
    'add', 'addu', 'addiu', 'dadd', 'daddu', 'daddiu',
    'sub', 'subu', 'dsub', 'dsubu',
    'and', 'andi', 'or', 'ori', 'xor', 'xori', 'nor',
    'sll', 'srl', 'sra', 'sllv', 'srlv', 'srav',
    'dsll', 'dsrl', 'dsra', 'dsllv', 'dsrlv', 'dsrav',
    'slt', 'sltu', 'slti', 'sltiu',
    'mul', 'mulo', 'mulou',
    'abs', 'neg', 'negu',
    'ext', 'ins',
    'mflo', 'mfhi', 'mfc0', 'mfc1', 'mfc2',
    'lui', 'li', 'la', 'move', 'movz', 'movn'
)
_WRITE_RE = re.compile(
    r'^\s*(?:' + '|'.join(_ALU_OPS) + 
    r'|lw|lh|lb|lhu|lbu|ld|lwl|lwr|ldl|ldr|lwc1|ldc1)\s+\$(\w+)'
)


def _parse_offset(raw):
    raw = raw.strip()
    if raw.startswith("("):
        m = re.search(r'(-?0x[\da-fA-F]+|-?\d+)', raw)
        if m:
            raw = m.group(1)
    if raw.startswith("-0x") or raw.startswith("-0X"):
        return -int(raw[1:], 16)
    if raw.startswith("0x") or raw.startswith("0X"):
        return int(raw, 16)
    return int(raw)


def _flush(curr, funcs, env_s, env_ext, file_sections):
    if curr is None:
        return

    # ------------------------------------------------------------------ stack
    saved      = {}
    raw_locals = []
    for off_hex, val in curr["stk"].items():
        if val == "local":
            raw_locals.append(int(off_hex, 16))
        else:
            saved[val] = off_hex

    raw_locals.sort()
    blocks  = []
    singles = 0
    if raw_locals:
        blk_start = raw_locals[0]
        blk_end   = raw_locals[0]
        for off in raw_locals[1:]:
            if off - blk_end <= 8:
                blk_end = off
            else:
                size = blk_end - blk_start + 4
                if size > 4:
                    blocks.append({"off": hex(blk_start), "size": size})
                else:
                    singles += 1
                blk_start = blk_end = off
        size = blk_end - blk_start + 4
        if size > 4:
            blocks.append({"off": hex(blk_start), "size": size})
        else:
            singles += 1

    locals_out = {}
    if blocks:
        locals_out["blocks"] = blocks
    if singles:
        locals_out["singles"] = singles

    curr["stk"] = {"saved": saved, "locals": locals_out}

    # ------------------------------------------------------------------ mem
    NOISE_REGS = {
        'v0','v1',
        't0','t1','t2','t3','t4','t5','t6','t7','t8','t9',
        'a0','a1','a2','a3',
        'k0','k1','at','zero',
    }
    _SP_PREFIX = re.compile(r'^sp_(0x[\da-fA-F]+)$')

    block_ranges = [(int(b["off"], 16), int(b["off"], 16) + b["size"])
                    for b in locals_out.get("blocks", [])]

    struct_patterns = {}
    for sname, sfields in env_s.items():
        s_offs = sorted(sfields.keys(), key=lambda x: int(x, 16))
        if s_offs:
            struct_patterns[sname] = (s_offs, int(s_offs[-1], 16) + 4)

    groups = {}
    for entry in curr["mem"]:
        base = entry["base"]
        sp_m = _SP_PREFIX.match(base)
        norm_base   = "sp"          if sp_m else base
        norm_sp_off = sp_m.group(1) if sp_m else None
        if norm_base in NOISE_REGS:
            continue
        struct_name = entry.get("struct", "")
        rw          = entry.get("rw", "r")
        key = (norm_base, norm_sp_off or "", struct_name)
        grp = groups.setdefault(key, {"fields": {}})
        grp["fields"].setdefault(entry["off"], set()).add(rw)

    SAVED_SET = {'s0','s1','s2','s3','s4','s5','s6','s7','s8','fp','gp'}
    clean_mem = []
    for (base, sp_off, struct_name), grp in groups.items():
        field_rw = grp["fields"]
        sf       = sorted(field_rw.keys(), key=lambda x: int(x, 16))

        if base == "sp" and sf == ["0x0"]:
            continue

        e = {"base": base}
        if sp_off:
            e["off"] = sp_off

        fields_out = []
        for f in sf:
            modes  = field_rw[f]
            rw_val = "rw" if len(modes) == 2 else modes.pop()
            fields_out.append({"off": f, "rw": rw_val})
        e["fields"] = fields_out

        lo       = int(sf[0],  16)
        hi       = int(sf[-1], 16)
        acc_size = hi - lo + 4
        if len(sf) > 1:
            e["size"] = acc_size

        if any(int(f, 16) % 4 != 0 for f in sf):
            e["packed"] = True

        if not struct_name and len(sf) > 1:
            for sname, (s_offs, s_size) in struct_patterns.items():
                if (all(f in s_offs for f in sf)
                        and abs(acc_size - s_size) <= 8):
                    struct_name = sname
                    break
        if struct_name:
            e["struct"] = struct_name

        if base == "sp" and sp_off:
            sp_val = int(sp_off, 16)
            if any(b_s <= sp_val < b_e for b_s, b_e in block_ranges):
                e["in_block"] = True

        clean_mem.append(e)

    def _mem_sort_key(e):
        b = e["base"]
        if b == "sp":      return (1, e.get("off", ""))
        if b in SAVED_SET: return (2, b)
        return (0, b)

    clean_mem.sort(key=_mem_sort_key)
    curr["mem"] = clean_mem

    # ---------------------------------------------------------------- calls
    reg_origins = curr.pop("_reg_origins", {})
    seen_calls  = set()
    clean_calls = []
    for c in curr["calls"]:
        if c["type"] == "direct":
            key = ("d", c["name"])
            if key not in seen_calls:
                seen_calls.add(key)
                clean_calls.append(c)
        else:
            reg  = c["reg"]
            hint = reg_origins.get(reg, "reg")
            hint = hint if hint in ("symbol", "fp") else "reg"
            entry = {"type": "indirect", "reg": reg, "hint": hint,
                     "argc": c.get("argc", 0)}
            key   = ("i", reg, hint)
            if key not in seen_calls:
                seen_calls.add(key)
                clean_calls.append(entry)

    curr["calls"] = sorted(clean_calls,
                           key=lambda c: (c["type"], c.get("name","") or c.get("reg","")))

    # ------------------------------------------------------------------ br
    LIKELY_OPCODES = {'beql','bnel','blezl','bgtzl','bltzl','bgezl'}
    label_pos    = curr.pop("_label_pos",    {})
    branch_sites = curr.pop("_branch_sites", [])

    br_map = {}
    for line_idx, opcode, target in branch_sites:
        target_pos  = label_pos.get(target)
        is_backward = target_pos is not None and target_pos < line_idx
        e = br_map.setdefault(opcode, {"op": opcode})
        if opcode in LIKELY_OPCODES:
            e["likely"] = True
        if is_backward:
            e["loop"] = True

    curr["br"] = sorted(br_map.values(), key=lambda x: x["op"])

    # ---------------------------------------------------------------- args/ret
    arg_read          = curr.pop("_arg_read",      set())
    arg_save_only     = curr.pop("_arg_save_only",  set())
    arg_save_reloaded = curr.pop("_arg_save_reloaded", set())
    arg_taint_used    = curr.pop("_arg_taint_used", set())
    curr.pop("_arg_save_slots", None)
    curr.pop("_arg_taint", None)
    curr.pop("_sp_loads",       None)
    curr.pop("_sp_loads_before_call", None)
    ext_sig  = env_ext.get(curr["n"], "")
    curr.pop("_arg_types",   None)
    curr.pop("_arg_written", None)
    first_call_seen = curr.pop("_first_call_seen", False)
    curr.pop("_delay_slot_next", None)
    curr.pop("_f12_written", None)
    curr.pop("_f14_written", None)

    for a in arg_save_only:
        arg_read.add(a)
    for a in arg_taint_used:
        arg_read.add(a)

    highest_genuine = -1
    for i, a in enumerate(['a0', 'a1', 'a2', 'a3']):
        if a in arg_read:
            highest_genuine = i
    for i, a in enumerate(['a0', 'a1', 'a2', 'a3']):
        if i <= highest_genuine:
            arg_read.add(a)

    if ext_sig:
        sig_m = re.match(r'\(([^)]*)\)\s*->\s*(\S+)', ext_sig)
        if sig_m:
            params_raw = [p.strip() for p in sig_m.group(1).split(",")
                          if p.strip() and p.strip() != "void"]
            ret_raw    = sig_m.group(2).strip()
            reg_names  = ['a0','a1','a2','a3']
            curr["args"] = [{"reg": reg_names[i], "type": t}
                            for i, t in enumerate(params_raw[:4])]
            curr["ret"]  = ret_raw
        else:
            curr["args"] = []
            curr["ret"]  = "void"
        curr["argc_conf"] = "high"
        curr["ret_conf"]  = "high"
    else:
        highest = -1
        for i, a in enumerate(['a0','a1','a2','a3']):
            if a in arg_read:
                highest = i
        args = ['a0','a1','a2','a3'][:highest + 1] if highest >= 0 else []
        curr["args"] = args

        has_verified_saves  = bool(arg_save_reloaded)
        has_taint_confirmed = bool(arg_taint_used)
        has_calls = first_call_seen

        if highest >= 0:
            if has_verified_saves or has_taint_confirmed:
                curr["argc_conf"] = "high"
            else:
                curr["argc_conf"] = "medium"
        else:
            if has_calls:
                curr["argc_conf"] = "medium" 
            else:
                curr["argc_conf"] = "high" 

        if first_call_seen:
            insns_after = curr.pop("_insn_after_last_call", 0)
            v0_explicit = curr.pop("_v0_set_after_last_call", False)
            last_call_name = curr.pop("_last_call_name", None)
            if v0_explicit:
                is_ret = True
                ret_conf = "high"
            elif insns_after == 0 and last_call_name:
                sub_sig = env_ext.get(last_call_name, "")
                if sub_sig:
                    ret_part = sub_sig.split("->")[-1].strip() if "->" in sub_sig else ""
                    is_ret = (ret_part != "void" and ret_part != "")
                    ret_conf = "medium"
                else:
                    is_ret = True
                    ret_conf = "low"
            else:
                is_ret = False
                ret_conf = "medium"
        else:
            is_ret = curr.pop("_v0_written", False)
            ret_conf = "high"
            
        curr.pop("_v0_set_after_last_call", None)
        curr.pop("_last_call_idx", None)
        curr.pop("_last_call_name", None)
        curr.pop("_insn_after_last_call", None)

        v0_taint = curr.pop("_v0_taint", "none")
        if is_ret and first_call_seen and v0_taint == "call":
            if not (insns_after == 0 and last_call_name):
                is_ret = False
                ret_conf = "medium"

        a0_struct_ret = curr.pop("_a0_struct_ret", False)
        curr.pop("_a0_modified", None)
        if a0_struct_ret and not is_ret:
            is_ret = True
            ret_conf = "medium"

        curr["ret"]  = "v0" if is_ret else "void"
        curr["ret_conf"] = ret_conf

    # ---------------------------------------------------------------- flags
    raw_flags = {
        "has_fpu":   curr.pop("_has_fp",    False),
        "has_div":   curr.pop("_has_div",   False),
        "has_mul":   curr.pop("_has_mul",   False),
        "has_64bit": curr.pop("_has_64bit", False),
    }
    curr.pop("_call_args_set", None)
    active = {k: True for k, v in raw_flags.items() if v}
    if active:
        curr["flags"] = active

    # ---------------------------------------------------------------- globals
    raw_grw = curr.pop("_global_rw", {})
    curr.pop("_call_arg_desc", None)
    if raw_grw:
        valid_globals = {}
        for sym, modes in raw_grw.items():
            valid_globals[sym] = ("rw" if len(modes) == 2 else next(iter(modes)))
        if valid_globals:
            curr["globals"] = valid_globals

    funcs.append(curr)


def analyze_asm_semantic(asm_path, env_s, env_sym, env_ext, file_sections):
    funcs = []
    if not os.path.exists(asm_path):
        return funcs
        
    _reject_set = set()
    _accept_set = set()
    if file_sections:
        _reject_set = set(file_sections.get(".rodata", [])) | set(file_sections.get(".text", []))
        _accept_set = (
            set(file_sections.get(".data", [])) |
            set(file_sections.get(".bss", [])) |
            set(file_sections.get(".sdata", [])) |
            set(file_sections.get(".sbss", [])) |
            set(file_sections.get(".comm", [])) |
            set(file_sections.get(".lcomm", [])) |
            set(file_sections.get("*COM*", []))
        )

    MIPS_REGS = {'v0','v1','a0','a1','a2','a3','t0','t1','t2','t3','t4','t5','t6','t7',
                 's0','s1','s2','s3','s4','s5','s6','s7','t8','t9','k0','k1',
                 'gp','sp','fp','ra','at','zero'}

    def _is_gvar(s):

        # Basic validity for C identifiers (filters out .LC0, $L12, etc.)
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', s):
            return False
     
        # Filter pure local code labels (often L followed by hex/digits)
        if re.match(r'^L[0-9A-Fa-f]+$', s):
            return False
        
        # Handle auto-generated D_ symbols
        if s.startswith("D_") or s.startswith("B_"):
            # 1. Rigorously filter known struct-offset patterns
            if s.startswith("D_00") or s.startswith("D_STR_00") or s.startswith("B_00"):
                return False
                
            # 2. Check whether the string contains a valid N64 RAM address (80... or A0...).
            # Covers D_80... as well as D_global_asm_80... or D_arcade_80...
            if re.search(r'_(8[0-3]|A0|a0)[0-9A-Fa-f]{6}', s) or s.startswith("D_80") or s.startswith("D_A0") or s.startswith("B_80") or s.startswith("B_A0"):
                return True
                
            # Block everything else
            return False

        # file_sections is the definitive source: if a symbol is in
        # _accept_set it IS a global — even if its name happens to match
        # a MIPS register (e.g. 'zero' as a math constant or 'fp' as a variable).
        if file_sections:
            if s in _reject_set:
                return False
            if s in _accept_set:
                return True

        # MIPS_REGS check only as fallback for the env_sym path.
        # A C variable name like 'a0' could collide with a register here —
        # without section data the filter is necessary.
        if s in MIPS_REGS:
            return False
            
        return s in env_sym


    with open(asm_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    curr        = None
    reg_map     = {}
    reg_origins = {}
    line_idx    = 0

    ARG_REGS = ['a0', 'a1', 'a2', 'a3']

    for line in lines:
        raw = line.strip()

        m = _RE_GLABEL.match(raw)
        if m:
            if curr is not None:
                _flush(curr, funcs, env_s, env_ext, file_sections)
            curr = {
                "n": m.group(1), "sf": 0, "stk": {}, "mem": [],
                "br": [], "calls": [],
                "_arg_read":      set(),
                "_arg_written":   set(),
                "_arg_types":     {},
                "_v0_written":    False,
                "_v0_set_after_last_call": False,
                "_v0_taint": "none",
                "_a0_modified":   False,
                "_a0_struct_ret": False,
                "_last_call_idx": -1,
                "_last_call_name": None,
                "_insn_after_last_call": 0,
                "_reg_origins":   {},
                "_label_pos":     {},
                "_branch_sites":  [],
                "_has_fp":        False,
                "_has_div":       False,
                "_has_mul":       False,
                "_has_64bit":     False,
                "_call_args_set": set(),
                "_global_rw":     {},
                "_call_arg_desc": {},
                "_arg_save_slots": {},
                "_arg_save_only":  set(),
                "_arg_save_reloaded": set(),
                "_arg_taint":      {},
                "_arg_taint_used": set(),
                "_sp_loads":       set(),
                "_sp_loads_before_call": set(),
                "_first_call_seen": False,
                "_delay_slot_next": False,
                "_f12_written":    False,
                "_f14_written":    False,
            }
            reg_map     = {}
            reg_origins = {}
            line_idx    = 0
            continue

        if curr is None:
            continue

        if _RE_ENDLABEL.match(raw):
            _flush(curr, funcs, env_s, env_ext, file_sections)
            curr = None
            continue

        clean = _RE_HEX_PFX.sub('', raw)
        clean = _RE_COMMENT.sub('', clean).strip()
        if not clean:
            continue

        in_delay_slot = curr["_delay_slot_next"]
        curr["_delay_slot_next"] = False
        
        # Track pointer updates to prevent _WRITE_RE from immediately clearing them again
        updated_reg = None

        m = _RE_LUI.search(clean)
        if m:
            reg_map[m.group(1)]     = m.group(2)
            reg_origins[m.group(1)] = "symbol"
            updated_reg = m.group(1)
            # lui %hi(sym): loads the address of a symbol.
            # Analogous to %got(sym) — counts as a global access.
            if _is_gvar(m.group(2)):
                curr["_global_rw"].setdefault(m.group(2), set()).add("r")

        m = _RE_ADDIU_SYM.search(clean)
        if m:
            reg_map[m.group(1)]     = m.group(2)
            reg_origins[m.group(1)] = "symbol"
            updated_reg = m.group(1)

        m = _RE_LA.search(clean)
        if m:
            reg_map[m.group(1)]     = m.group(2)
            reg_origins[m.group(1)] = "symbol"
            updated_reg = m.group(1)
            # la $reg, sym: pseudo-instruction to load a symbol address.
            # Counts as a global access, analogous to %got.
            if _is_gvar(m.group(2)):
                curr["_global_rw"].setdefault(m.group(2), set()).add("r")

        for pat in (_RE_MOVE, _RE_ADDU_ZERO):
            m = pat.search(clean)
            if m:
                dst = m.group(1)
                src = m.group(2)
                if src in reg_map:
                    reg_map[dst]     = reg_map[src]
                    reg_origins[dst] = reg_origins.get(src, "reg")
                else:
                    reg_map.pop(dst, None)
                    reg_origins.pop(dst, None)
                updated_reg = dst
                break

        m = _RE_LOAD_SP.search(clean)
        if m:
            off_hex = hex(_parse_offset(m.group(3)))
            reg_map[m.group(2)]     = f"sp_{off_hex}"
            reg_origins[m.group(2)] = "fp"
            updated_reg = m.group(2)

        m = _RE_LOAD_NONSP.search(clean)
        if m:
            reg_origins[m.group(1)] = "fp"
            # _WRITE_RE handles the pop automatically here since updated_reg = None

        m_c16 = re.search(r'\blw\s+\$(\w+),\s*%call16\((\w+)\)\(\$gp\)', clean)
        if m_c16:
            reg_map[m_c16.group(1)]     = m_c16.group(2)
            reg_origins[m_c16.group(1)] = "symbol"
            updated_reg = m_c16.group(1)

        m_sp_load = re.search(r'\blw\s+\$\w+,\s*(0x[\da-fA-F]+|-?\d+|\(0x[\da-fA-F]+\s*&\s*0xFFFF\))\(\$sp\)', clean)
        if m_sp_load:
            off = _parse_offset(m_sp_load.group(1))
            curr["_sp_loads"].add(off)
            if not curr["_first_call_seen"]:
                curr["_sp_loads_before_call"].add(off)

        m_got = re.search(r'\blw\s+\$(\w+),\s*%got\((\w+)(?:\s*\+\s*\S+)?\)\(\$gp\)', clean)
        if m_got:
            sym = m_got.group(2)
            reg = m_got.group(1)
            reg_map[reg]     = sym
            reg_origins[reg] = "symbol"
            updated_reg = reg
            # If the symbol is a global, loading its address via %got already counts
            # as an access. In C this corresponds to &var (passing an address to a
            # function) or a direct read/write. Without this check, globals that are
            # ONLY passed as an address — without a subsequent %lo access — would
            # be missed.
            if _is_gvar(sym):
                curr["_global_rw"].setdefault(sym, set()).add("r")

        m_lo = re.search(
            r'\b(lw|sw|lh|sh|lb|sb|lhu|lbu|ld|sd|lwc1|swc1|ldc1|sdc1)\s+'
            r'\$(\w+),\s*%lo\((\w+)\)\(\$(\w+)\)', clean)
        if m_lo:
            lo_op, lo_target, lo_sym, lo_base = m_lo.groups()
            if lo_op.startswith("l"):
                reg_map[lo_target]     = lo_sym
                reg_origins[lo_target] = "symbol"
                updated_reg = lo_target
            if _is_gvar(lo_sym):
                grw = "r" if lo_op.startswith("l") else "w"
                curr["_global_rw"].setdefault(lo_sym, set()).add(grw)

        m_gpr = re.search(r'\b(?:lw|lh|lb|lhu|lbu|ld|lwc1|ldc1|sw|sh|sb|sd|swc1|sdc1)\s+'
                          r'\$(\w+),\s*%gp_rel\((\w+)\)\(\$gp\)', clean)
        if m_gpr:
            gpr_op  = clean.split()[0].lower()
            gpr_reg = m_gpr.group(1)
            gpr_sym = m_gpr.group(2)
            
            if gpr_op.startswith("l"):
                reg_map[gpr_reg]     = gpr_sym
                reg_origins[gpr_reg] = "symbol"
                updated_reg = gpr_reg
                
            if _is_gvar(gpr_sym):
                grw = "r" if gpr_op.startswith("l") else "w"
                curr["_global_rw"].setdefault(gpr_sym, set()).add(grw)

        m_addiu_gp = re.search(r'\b(?:d?addiu|addu?)\s+\$(\w+),\s+\$gp,\s*%gp_rel\((\w+)\)', clean)
        if m_addiu_gp:
            reg_map[m_addiu_gp.group(1)]     = m_addiu_gp.group(2)
            reg_origins[m_addiu_gp.group(1)] = "symbol"
            updated_reg = m_addiu_gp.group(1)
            # addiu $reg, $gp, %gp_rel(sym): address of a GP-relative symbol.
            # Counts as a global access (address is computed for &var passing).
            if _is_gvar(m_addiu_gp.group(2)):
                curr["_global_rw"].setdefault(m_addiu_gp.group(2), set()).add("r")

        curr["_reg_origins"] = dict(reg_origins)

        m_gwrw = _RE_MEM.search(clean)
        if m_gwrw:
            gop, target_reg, _goff, gbase = m_gwrw.groups()
            
            # Only when the base register is tracked does this refer to a symbol
            if gbase in reg_map:
                gbase_name = reg_map[gbase]
                if _is_gvar(gbase_name) and gbase != "sp":
                    grw = "r" if gop.lower().startswith("l") else "w"
                    curr["_global_rw"].setdefault(gbase_name, set()).add(grw)

        m_ca = _RE_ARG_SET.search(clean)
        if m_ca:
            areg = m_ca.group(1)
            mm   = _RE_MEM.search(clean)
            if mm and mm.group(4) != "sp":
                src_base = reg_map.get(mm.group(4), mm.group(4))
                curr["_call_arg_desc"][areg] = f"{src_base}+{mm.group(3)}"
            else:
                sym_val = reg_map.get(areg)
                if sym_val and not sym_val.startswith("sp_"):
                    curr["_call_arg_desc"][areg] = sym_val
                else:
                    curr["_call_arg_desc"][areg] = None

        first_tok = clean.split()[0] if clean.split() else ""
        if first_tok in ('lwc1','swc1','ldc1','sdc1','add.s','sub.s','mul.s',
                         'div.s','add.d','sub.d','mul.d','div.d','cvt.s.w',
                         'cvt.w.s','cvt.d.w','cvt.w.d','mfc1','mtc1','c.lt.s',
                         'c.le.s','c.eq.s','c.lt.d','c.le.d','c.eq.d','bc1t','bc1f'):
            curr["_has_fp"] = True
        if first_tok in ('div','divu','ddiv','ddivu'):
            curr["_has_div"] = True
            if first_tok.startswith('dd'):
                curr["_has_64bit"] = True
        if first_tok in ('mult','multu','dmult','dmultu','mul'):
            curr["_has_mul"] = True
            if first_tok.startswith('dm'):
                curr["_has_64bit"] = True
        if first_tok in ('dadd','daddu','dsub','dsubu','dsll','dsrl','dsra',
                         'ld','sd','ldc1','sdc1'):
            curr["_has_64bit"] = True

        m = _RE_SF.search(clean)
        if m:
            curr["sf"] = _parse_offset(m.group(1))

        m_aset = _RE_ARG_SET.search(clean)
        if m_aset:
            curr["_call_args_set"].add(m_aset.group(1))

        m = _RE_JAL.search(clean)
        if m:
            call_args = set(curr["_call_args_set"])
            argc = 0
            for i, ar in enumerate(['a0','a1','a2','a3']):
                if ar in call_args:
                    argc = i + 1
            ctx = {ar: desc for ar, desc in curr["_call_arg_desc"].items()
                   if desc is not None and ar in call_args}
            entry = {"type": "direct", "name": m.group(1), "argc": argc}
            if ctx:
                entry["ctx"] = ctx
            curr["calls"].append(entry)
            
            if not curr["_first_call_seen"]:
                highest_call_arg = -1
                for i, ar in enumerate(['a0','a1','a2','a3']):
                    if ar in call_args:
                        highest_call_arg = i
                sig_argc = -1
                called_name = m.group(1)
                called_sig = env_ext.get(called_name, "")
                if called_sig:
                    sig_m2 = re.match(r'\(([^)]*)\)', called_sig)
                    if sig_m2:
                        params = [p.strip() for p in sig_m2.group(1).split(",")
                                  if p.strip() and p.strip() != "void"]
                        sig_argc = min(len(params), 4) - 1
                highest_touched = -1
                for i, ar in enumerate(['a0','a1','a2','a3']):
                    if ar in call_args or ar in curr["_arg_written"] or ar in curr["_arg_read"]:
                        highest_touched = i
                sf = curr.get("sf", 0)
                if sf > 0 and curr.get("_sp_loads_before_call"):
                    for soff in curr["_sp_loads_before_call"]:
                        if soff >= sf + 0x10:
                            highest_touched = max(highest_touched, 3)
                            break
                inferred_top = max(highest_touched, highest_call_arg, sig_argc)
                for i, ar in enumerate(['a0','a1','a2','a3']):
                    if i > inferred_top:
                        break
                    if ar not in curr["_arg_written"]:
                        curr["_arg_read"].add(ar)
            curr["_call_args_set"] = set()
            curr["_call_arg_desc"] = {}
            curr["_first_call_seen"] = True
            curr["_delay_slot_next"] = True
            curr["_v0_set_after_last_call"] = False
            curr["_v0_taint"] = "call"
            curr["_insn_after_last_call"] = 0
            curr["_last_call_name"] = entry.get("name")

        m = _RE_JALR.search(clean)
        if m:
            reg  = m.group(1)
            call_args = set(curr["_call_args_set"])
            argc = 0
            for i, ar in enumerate(['a0','a1','a2','a3']):
                if ar in call_args:
                    argc = i + 1
            ctx = {ar: desc for ar, desc in curr["_call_arg_desc"].items()
                   if desc is not None and ar in call_args}

            resolved_name = reg_map.get(reg)
            origin        = reg_origins.get(reg, "reg")

            if resolved_name and origin == "symbol":
                entry = {"type": "direct", "name": resolved_name, "argc": argc}
            else:
                hint  = origin if origin in ("symbol", "fp") else "reg"
                entry = {"type": "indirect", "reg": reg, "hint": hint, "argc": argc}

            if ctx:
                entry["ctx"] = ctx
            curr["calls"].append(entry)
            
            if not curr["_first_call_seen"]:
                highest_call_arg = -1
                for i, ar in enumerate(['a0','a1','a2','a3']):
                    if ar in call_args:
                        highest_call_arg = i
                sig_argc = -1
                called_name = resolved_name if (resolved_name and origin == "symbol") else None
                if called_name:
                    called_sig = env_ext.get(called_name, "")
                    if called_sig:
                        sig_m2 = re.match(r'\(([^)]*)\)', called_sig)
                        if sig_m2:
                            params = [p.strip() for p in sig_m2.group(1).split(",")
                                      if p.strip() and p.strip() != "void"]
                            sig_argc = min(len(params), 4) - 1
                highest_touched = -1
                for i, ar in enumerate(['a0','a1','a2','a3']):
                    if ar in call_args or ar in curr["_arg_written"] or ar in curr["_arg_read"]:
                        highest_touched = i
                sf = curr.get("sf", 0)
                if sf > 0 and curr.get("_sp_loads_before_call"):
                    for soff in curr["_sp_loads_before_call"]:
                        if soff >= sf + 0x10:
                            highest_touched = max(highest_touched, 3)
                            break
                inferred_top = max(highest_touched, highest_call_arg, sig_argc)
                for i, ar in enumerate(['a0','a1','a2','a3']):
                    if i > inferred_top:
                        break
                    if ar not in curr["_arg_written"]:
                        curr["_arg_read"].add(ar)
            curr["_call_args_set"] = set()
            curr["_call_arg_desc"] = {}
            curr["_first_call_seen"] = True
            curr["_delay_slot_next"] = True
            curr["_v0_set_after_last_call"] = False
            curr["_v0_taint"] = "call"
            curr["_insn_after_last_call"] = 0
            curr["_last_call_name"] = entry.get("name")

        m_tc = _RE_TAIL_CALL.match(clean)
        if m_tc:
            target = m_tc.group(1)
            if target not in curr["_label_pos"]:
                argc = 0
                for i, ar in enumerate(['a0','a1','a2','a3']):
                    if ar in curr["_call_args_set"]:
                        argc = i + 1
                ctx = {ar: desc for ar, desc in curr["_call_arg_desc"].items()
                       if desc is not None and ar in curr["_call_args_set"]}
                entry = {"type": "direct", "name": target, "argc": argc,
                         "tail": True}
                if ctx:
                    entry["ctx"] = ctx
                curr["calls"].append(entry)
                curr["_call_args_set"] = set()
                curr["_call_arg_desc"] = {}

        m_addiu_sp = _RE_ADDIU_SP_ARG.search(clean)
        if m_addiu_sp:
            curr["_arg_written"].add(m_addiu_sp.group(1))

        if not curr["_f12_written"]:
            if (re.search(r'\bmtc1\s+\$\w+,\s*\$f12\b', clean) or
                re.search(r'\blwc1\s+\$f12\b', clean) or
                re.search(r'\bldc1\s+\$f12\b', clean) or
                (re.match(r'^(?:cvt|add|sub|mul|div|abs|neg|sqrt|mov|trunc|'
                          r'round|ceil|floor)', first_tok)
                 and re.search(r'^\S+\s+\$f12\b', clean))):
                curr["_f12_written"] = True
        if not curr["_f14_written"]:
            if (re.search(r'\bmtc1\s+\$\w+,\s*\$f14\b', clean) or
                re.search(r'\blwc1\s+\$f14\b', clean) or
                re.search(r'\bldc1\s+\$f14\b', clean) or
                (re.match(r'^(?:cvt|add|sub|mul|div|abs|neg|sqrt|mov|trunc|'
                          r'round|ceil|floor)', first_tok)
                 and re.search(r'^\S+\s+\$f14\b', clean))):
                curr["_f14_written"] = True

        if not curr["_first_call_seen"] or in_delay_slot:
            m_farg = re.search(
                r'\b(swc1|sdc1)\s+\$(f12|f14),\s*(?:0x[\da-fA-F]+|-?\d+|\(0x[\da-fA-F]+\s*&\s*0xFFFF\))\(\$sp\)', clean)
            if m_farg:
                freg = m_farg.group(2)
                if freg == "f12" and not curr["_f12_written"]:
                    curr["_arg_read"].add("a0")
                    if m_farg.group(1) == "sdc1":
                        curr["_arg_read"].add("a1")
                elif freg == "f14" and not curr["_f14_written"]:
                    curr["_arg_read"].add("a2")
                    if m_farg.group(1) == "sdc1":
                        curr["_arg_read"].add("a3")

            if re.search(r'\$f1[24]\b', clean):
                toks = clean.split()
                first_tok = toks[0] if toks else ""
                is_fpu_instr = (
                    first_tok not in ('swc1','sdc1','lwc1','ldc1') and
                    bool(re.match(r'^(?:add|sub|mul|div|abs|neg|sqrt|mov|cvt|trunc|round|'
                                  r'ceil|floor|c\.|bc1|mfc1|mtc1)', first_tok))
                )
                if is_fpu_instr:
                    is_double = bool(re.match(
                        r'^(?:cvt\.d|add\.d|sub\.d|mul\.d|div\.d|mov\.d|neg\.d|abs\.d|'
                        r'sqrt\.d|c\.\w+\.d)', first_tok))
                    is_compare = first_tok.startswith('c.')
                    is_mfc_mtc = first_tok in ('mfc1', 'mtc1')
                    operands = clean[len(first_tok):]
                    f_regs = re.findall(r'\$(f\d+)\b', operands)
                    if is_compare or is_mfc_mtc:
                        source_fregs = set(f_regs)
                    else:
                        source_fregs = set(f_regs[1:]) if len(f_regs) > 1 else set()
                    if "f12" in source_fregs and not curr["_f12_written"]:
                        curr["_arg_read"].add("a0")
                        if is_double:
                            curr["_arg_read"].add("a1")
                    if "f14" in source_fregs and not curr["_f14_written"]:
                        curr["_arg_read"].add("a2")
                        if is_double:
                            curr["_arg_read"].add("a3")

            m_fmov = re.search(r'\bmov\.[sd]\s+\$f\w+,\s+\$(f12|f14)\b', clean)
            if m_fmov:
                freg = m_fmov.group(1)
                is_double_mov = clean.lstrip().startswith("mov.d")
                if freg == "f12" and not curr["_f12_written"]:
                    curr["_arg_read"].add("a0")
                    if is_double_mov:
                        curr["_arg_read"].add("a1")
                elif freg == "f14" and not curr["_f14_written"]:
                    curr["_arg_read"].add("a2")
                    if is_double_mov:
                        curr["_arg_read"].add("a3")

            m_mtc1 = re.search(r'\bmtc1\s+\$(a[0-3]),\s*\$f\w+\b', clean)
            if m_mtc1:
                arg = m_mtc1.group(1)
                if arg not in curr["_arg_written"]:
                    curr["_arg_read"].add(arg)

            m_argsrc = re.search(
                r'\b(?:move|addu|addiu|subu|and|andi|or|ori|xor|xori|nor|'
                r'sll|srl|sra|sllv|srlv|srav|slt|sltu|slti|sltiu)\s+'
                r'\$(?!a[0-3]\b)(\w+),\s*\$(a[0-3])\b', clean)
            if m_argsrc:
                dest_reg = m_argsrc.group(1)
                arg = m_argsrc.group(2)
                if arg not in curr["_arg_written"]:
                    curr["_arg_read"].add(arg)
                    if re.match(r'^s[0-7]$', dest_reg):
                        curr["_arg_taint"][dest_reg] = arg

        for arg in ARG_REGS:
            if arg not in curr["_arg_written"]:
                m_sw = re.search(
                    r'\bsw\s+\$' + arg + r',\s*(0x[\da-fA-F]+|-?\d+|\(0x[\da-fA-F]+\s*&\s*0xFFFF\))\(\$sp\)',
                    clean)
                if m_sw:
                    off = _parse_offset(m_sw.group(1))
                    curr["_arg_save_slots"][off] = arg
                    if not curr["_first_call_seen"] or in_delay_slot:
                        curr["_arg_save_only"].add(arg)

        m_any_sw = re.search(
            r'\b(?:sw|sh|sb|sd|swc1|sdc1)\s+\$\w+,\s*(0x[\da-fA-F]+|-?\d+|\(0x[\da-fA-F]+\s*&\s*0xFFFF\))\(\$sp\)',
            clean)
        if m_any_sw:
            off_write = _parse_offset(m_any_sw.group(1))
            if off_write in curr["_arg_save_slots"]:
                if not re.search(r'\bsw\s+\$(a[0-3]),\s*(?:0x[\da-fA-F]+|-?\d+|\(0x[\da-fA-F]+\s*&\s*0xFFFF\))\(\$sp\)', clean):
                    del curr["_arg_save_slots"][off_write]

        for arg in ARG_REGS:
            if arg in curr["_arg_written"]:
                continue
            if re.search(r'\$' + arg + r'\b', clean):
                wm = _RE_ARG_WRITE.search(clean)
                if wm and wm.group(1) == arg:
                    if len(re.findall(r'\$' + arg + r'\b', clean)) > 1:
                        if not curr["_first_call_seen"] or in_delay_slot:
                            curr["_arg_read"].add(arg)
                            curr["_arg_save_only"].discard(arg)
                    else:
                        curr["_arg_written"].add(arg)
                else:
                    if not curr["_first_call_seen"] or in_delay_slot:
                        curr["_arg_read"].add(arg)
                        curr["_arg_save_only"].discard(arg)

        wm_any = re.match(r'\b(?:' + _WRITE_OPS + r')\s+\$([a-zA-Z0-9]+)\b', clean)
        if wm_any:
            dest = wm_any.group(1)
            if dest in curr["_arg_taint"]:
                if not re.search(r'\b(?:move|addu|or)\s+\$' + dest + r',\s*\$(a[0-3])\b', clean):
                    del curr["_arg_taint"][dest]

        if curr["_first_call_seen"] and curr["_arg_taint"]:
            for sreg, orig_arg in list(curr["_arg_taint"].items()):
                if re.search(r'\$' + sreg + r'\b', clean):
                    wm = re.match(r'\b(?:' + _WRITE_OPS + r')\s+\$' + sreg + r'\b', clean)
                    if wm:
                        if len(re.findall(r'\$' + sreg + r'\b', clean)) > 1:
                            curr["_arg_taint_used"].add(orig_arg)
                    else:
                        curr["_arg_taint_used"].add(orig_arg)

        if curr["_arg_save_slots"]:
            m_lw = re.search(
                r'\blw\s+\$\w+,\s*(0x[\da-fA-F]+|-?\d+|\(0x[\da-fA-F]+\s*&\s*0xFFFF\))\(\$sp\)', clean)
            if m_lw:
                off = _parse_offset(m_lw.group(1))
                if off in curr["_arg_save_slots"]:
                    curr["_arg_save_reloaded"].add(curr["_arg_save_slots"][off])

        if _RE_V0_WRITE.search(clean):
            is_got_load = bool(re.search(
                r'\blw\s+\$v0,\s*%(?:got|call16|gp_rel)\(', clean))
            if not is_got_load:
                curr["_v0_set_after_last_call"] = True
                curr["_v0_written"] = True
                curr["_v0_taint"] = "explicit"

        if not curr["_a0_modified"] and not curr["_a0_struct_ret"]:
            m_store_via_a0 = re.search(
                r'\b(?:sw|sh|sb|swc1|sdc1)\s+\$\w+,\s*(?:0x[\da-fA-F]+|-?\d+|\(0x[\da-fA-F]+\s*&\s*0xFFFF\))\(\$a0\)',
                clean)
            if m_store_via_a0:
                curr["_a0_struct_ret"] = True
                
        if not curr["_a0_modified"]:
            m_a0_write = re.search(
                r'\b(?:' + _WRITE_OPS + r')\s+\$a0\b', clean)
            if m_a0_write:
                if not re.search(r'\baddiu\s+\$a0,\s*\$a0\b', clean):
                    curr["_a0_modified"] = True

        if curr["_first_call_seen"] and not in_delay_slot:
            is_epilog_insn = bool(re.match(
                r'(?:lw\s+\$(?:ra|gp|s\d|f\d)|ldc1\s+\$f|d?addiu\s+\$sp|'
                r'jr\s+\$ra|nop)\b', clean))
            is_call_line = bool(re.match(r'jalr?\b', clean))
            if not is_epilog_insn and not is_call_line:
                curr["_insn_after_last_call"] += 1

        m = _RE_MEM.search(clean)
        if m:
            op, target_reg, off_raw, base_reg = m.groups()
            op      = op.lower()
            off_hex = hex(_parse_offset(off_raw))

            if base_reg == "sp":
                if target_reg in SAVED_REGS:
                    curr["stk"][off_hex] = target_reg
                else:
                    curr["stk"][off_hex] = "local"
            else:
                base_name = reg_map.get(base_reg, base_reg)
                rw        = "r" if op.startswith("l") else "w"
                mem_entry = {"base": base_name, "off": off_hex, "rw": rw}

                sym_type = env_sym.get(base_name, "")
                if "struct " in sym_type:
                    st_name = sym_type.replace("struct ", "").strip().lstrip("*").strip()
                    if st_name in env_s:
                        mem_entry["struct"] = st_name

                curr["mem"].append(mem_entry)

        line_idx += 1

        m_lbl = _RE_LABEL_DEF.match(clean)
        if m_lbl:
            curr["_label_pos"][m_lbl.group(1)] = line_idx

        m = _RE_BRANCH.match(clean)
        if m:
            opcode = m.group(1)
            curr["br"].append(opcode)
            tgt_m = _RE_BRANCH_TARGET.match(clean)
            if tgt_m:
                curr["_branch_sites"].append((line_idx, opcode, tgt_m.group(1)))

        # RIGOROUS REGISTER INVALIDATION (STALE POINTER TRACKING)
        m_write = _WRITE_RE.match(clean)
        if m_write:
            w_dst = m_write.group(1)
            if w_dst != updated_reg and w_dst in reg_map:
                reg_map.pop(w_dst, None)
                reg_origins.pop(w_dst, None)

    if curr is not None:
        _flush(curr, funcs, env_s, env_ext, file_sections)

    return funcs


# ---------------------------------------------------------------------------
# 4. WORKER & ORCHESTRATION
# ---------------------------------------------------------------------------

def worker(args):
    c_path, s_path, h_dir, out_path, file_sections = args
    try:
        s_map, sym_map, ext_map, enum_map = parse_env_expert(c_path, h_dir)
        funcs = analyze_asm_semantic(s_path, s_map, sym_map, ext_map, file_sections)

        if USE_M2C and M2C_AVAILABLE:
            with open(os.devnull, 'w') as fnull:
                with contextlib.redirect_stderr(fnull), contextlib.redirect_stdout(fnull):
                    try:
                        m2c_map = get_argc_ret_map(s_path)
                    except Exception:
                        m2c_map = None
                
            if m2c_map:
                for func in funcs:
                    fname = func["n"]
                    if fname in m2c_map:
                        m2c_info = m2c_map[fname]
                        ext_sig = ext_map.get(fname, "")

                        has_calls = bool(func.get("calls"))

                        if not ext_sig:
                            m2c_argc = m2c_info["argc"]
                            heur_argc = len(func.get("args", []))

                            if m2c_argc == heur_argc:
                                if m2c_argc == 0 and has_calls:
                                    func["argc_conf"] = "medium"
                            else:
                                reg_names = ['a0','a1','a2','a3']
                                func["args"] = reg_names[:m2c_argc]
                                func["argc_conf"] = "medium" 

                        m2c_ret = m2c_info.get("ret")
                        if m2c_ret is not None and not ext_sig:
                            heur_ret = func.get("ret", "void")
                            m2c_ret_str = "v0" if m2c_ret else "void"
                            if m2c_ret_str == heur_ret:
                                if heur_ret == "void" and has_calls:
                                    func["ret_conf"] = "medium"
                            else:
                                func["ret"] = m2c_ret_str
                                func["ret_conf"] = "medium" 

        env = {"s": s_map, "sym": sym_map, "ext": ext_map}
        if enum_map:
            env["e"] = enum_map
        result = {
            "file":  os.path.basename(s_path),
            "env":   env,
            "funcs": funcs,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        return True, None
    except Exception as e:
        return False, f"{os.path.basename(s_path)}: {e}"


def main():
    m2c_status = "ENABLED" if (USE_M2C and M2C_AVAILABLE) else "DISABLED"
    if USE_M2C and not M2C_AVAILABLE:
        m2c_status += " (m2c_argc not found, heuristic only)"
    print(f"m2c argc oracle: {m2c_status}")
    for group in GROUPS:
        print(f"\n--- SEMANTIC EXPERT EXTRACTOR: {group} ---")

        c_dir   = os.path.join(DATASET_DIR, group)
        s_dir   = os.path.join(DATASET_DIR, f"ASM_Raw_{group}")
        h_dir   = os.path.join(DATASET_DIR, f"{group}_headers")
        out_dir = os.path.join(DATASET_DIR, f"JSON_Expert_{group}")
        
        global_map_path = os.path.join(DATASET_DIR, f"global_symbols_{group}.jsonl")
        group_sections = {}
        if os.path.exists(global_map_path):
            with open(global_map_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip(): continue
                    data = json.loads(line)
                    group_sections[data["file"]] = data.get("sections", {})

        os.makedirs(out_dir, exist_ok=True)

        if not os.path.exists(s_dir):
            print(f"  [SKIP] ASM directory not found: {s_dir}")
            continue

        tasks = []
        for fname in sorted(os.listdir(s_dir)):
            if not fname.endswith(".s"):
                continue
            base = fname[:-2]
            tasks.append((
                os.path.join(c_dir,   f"{base}.c"),
                os.path.join(s_dir,   fname),
                h_dir,
                os.path.join(out_dir, f"{base}.json"),
                group_sections.get(fname, {})
            ))

        if not tasks:
            print("  [SKIP] No .s files found.")
            continue

        print(f"  Found {len(tasks)} assembly files to process.")
        errors = []
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool() as pool:
            for ok, err in tqdm(pool.imap_unordered(worker, tasks),
                                total=len(tasks), desc=group):
                if not ok and err:
                    errors.append(err)

        if errors:
            print(f"  [{len(errors)} errors]")
            for e in errors[:10]:
                print(f"    {e}")
            if len(errors) > 10:
                print(f"    ... and {len(errors) - 10} more")
        else:
            print(f"  [OK] All {len(tasks)} files processed successfully.")


if __name__ == "__main__":
    main()
