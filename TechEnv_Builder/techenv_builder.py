# -*- coding: utf-8 -*-
"""
Semantic Expert Extractor  v10 (Single-Pair Mode)
Changes vs v9:
  - Removed hardcoded dataset paths and group iteration.
  - Input:  .s files from Input_ASM/
  - Input:  .h files from Input_Header/ (must match .s basename)
  - Output: JSON files to Output_JSON/
  - Removed .c file dependency; header is parsed directly.
"""

import os
import re
import json
import multiprocessing
from tqdm import tqdm
from pycparser import c_parser, c_ast, c_generator

# ---------------------------------------------------------------------------
# 1. CONFIGURATION
# ---------------------------------------------------------------------------

INPUT_ASM    = "Input_ASM"
INPUT_HEADER = "Input_Header"
OUTPUT_DIR   = "Output_JSON"

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


def _extract_enum(en_node, raw_enums):
    """
    Populate raw_enums[enum_name] = {int_value: enumerator_name} from a
    c_ast.Enum node.  Handles explicit values and auto-increment.
    """
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


def parse_env_expert(h_path, h_dir):
    """
    Parse a single header file (and its local includes from h_dir).
    Returns: s_map, sym_map, ext_map, enum_map
    """
    s_map, sym_map, ext_map, enum_map = {}, {}, {}, {}
    if not os.path.exists(h_path):
        return s_map, sym_map, ext_map, enum_map

    with open(h_path, "r", encoding="utf-8", errors="replace") as f:
        src = f.read()

    incs = re.findall(r'#include\s+"([^"]+)"', src)
    incs.append(os.path.basename(h_path))

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
            _strip_declname(f.type)
            t_str = gen.visit(f.type).strip()
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
                _strip_declname(f.type)
                t_str = gen.visit(f.type).strip()
                sz = _type_size(t_str, struct_sizes)
                offset = _align(offset, sz)
                s_map[st_name][hex(offset)] = t_str
                offset += sz
            continue

        if not node.name or node.name in BUILTIN_NAMES:
            continue

        if isinstance(node.type, c_ast.FuncDecl):
            fd = node.type
            _strip_declname(fd.type)
            ret = gen.visit(fd.type).strip()
            params = []
            if fd.args:
                for p in fd.args.params:
                    if isinstance(p, c_ast.EllipsisParam):
                        params.append("...")
                    else:
                        _strip_declname(p.type)
                        params.append(gen.visit(p.type).strip())
            params_str = ", ".join(params) if params else "void"
            ext_map[node.name] = f"({params_str}) -> {ret}"
            continue

        if 'typedef' not in (node.storage or []):
            if re.match(r'^sp[0-9A-Fa-f]+$', node.name):
                continue
            _strip_declname(node.type)
            sym_map[node.name] = gen.visit(node.type).strip()

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
_RE_LOAD_SP       = re.compile(r'\b(lw|ld)\s+\$(\w+),\s*(0x[\da-fA-F]+|-?\d+)\(\$sp\)')
_RE_LOAD_NONSP    = re.compile(r'\blw\s+\$(\w+),\s*(?:0x[\da-fA-F]+|-?\d+)\(\$(?!sp)(\w+)\)')

_RE_SF            = re.compile(r'addiu\s+\$sp,\s+\$sp,\s+-(0x[\da-fA-F]+|\d+)')
_RE_JAL           = re.compile(r'jal\s+(\w+)')
_RE_JALR          = re.compile(r'jalr\s+\$(\w+)')
_RE_TAIL_CALL     = re.compile(r'^j\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*$')

_RE_MEM = re.compile(
    r'\b(l[bBhHwWdD]u?|s[bBhHwWdD]|lwc1|swc1|ldc1|sdc1)\s+'
    r'\$(\w+),\s*(0x[\da-fA-F]+|-?\d+)\(\$(\w+)\)'
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
    r'or|nor|and|xor|mflo|mfhi|sll|srl|sra|dsll|dsrl|dsra'
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


def _parse_offset(raw):
    raw = raw.strip()
    if raw.startswith("0x") or raw.startswith("0X"):
        return int(raw, 16)
    return int(raw)


def _flush(curr, funcs, env_s, env_ext):
    if curr is None:
        return

    # ------------------------------------------------------------------ stk
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
    arg_read      = curr.pop("_arg_read",      set())
    arg_save_only = curr.pop("_arg_save_only",  set())
    curr.pop("_arg_save_slots", None)
    curr.pop("_sp_loads",       None)
    ext_sig  = env_ext.get(curr["n"], "")
    curr.pop("_arg_types",   None)
    curr.pop("_arg_written", None)

    highest_genuine = -1
    for i, a in enumerate(['a0', 'a1', 'a2', 'a3']):
        if a in arg_read:
            highest_genuine = i

    for i, a in enumerate(['a0', 'a1', 'a2', 'a3']):
        if a in arg_save_only:
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
    else:
        highest = -1
        for i, a in enumerate(['a0','a1','a2','a3']):
            if a in arg_read:
                highest = i
        args = ['a0','a1','a2','a3'][:highest + 1] if highest >= 0 else []
        curr["args"] = args
        curr["ret"]  = "v0" if curr.pop("_v0_written", False) else "void"

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
        curr["globals"] = {
            sym: ("rw" if len(modes) == 2 else next(iter(modes)))
            for sym, modes in raw_grw.items()
        }

    funcs.append(curr)


def analyze_asm_semantic(asm_path, env_s, env_sym, env_ext):
    funcs = []
    if not os.path.exists(asm_path):
        return funcs

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
                _flush(curr, funcs, env_s, env_ext)
            curr = {
                "n": m.group(1), "sf": 0, "stk": {}, "mem": [],
                "br": [], "calls": [],
                "_arg_read":      set(),
                "_arg_written":   set(),
                "_arg_types":     {},
                "_v0_written":    False,
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
                "_sp_loads":       set(),
            }
            reg_map     = {}
            reg_origins = {}
            line_idx    = 0
            continue

        if curr is None:
            continue

        if _RE_ENDLABEL.match(raw):
            _flush(curr, funcs, env_s, env_ext)
            curr = None
            continue

        clean = _RE_HEX_PFX.sub('', raw)
        clean = _RE_COMMENT.sub('', clean).strip()
        if not clean:
            continue

        m = _RE_LUI.search(clean)
        if m:
            reg_map[m.group(1)]     = m.group(2)
            reg_origins[m.group(1)] = "symbol"

        m = _RE_ADDIU_SYM.search(clean)
        if m:
            reg_map[m.group(1)]     = m.group(2)
            reg_origins[m.group(1)] = "symbol"

        m = _RE_LA.search(clean)
        if m:
            reg_map[m.group(1)]     = m.group(2)
            reg_origins[m.group(1)] = "symbol"

        for pat in (_RE_MOVE, _RE_ADDU_ZERO):
            m = pat.search(clean)
            if m:
                src = m.group(2)
                if src in reg_map:
                    reg_map[m.group(1)]     = reg_map[src]
                    reg_origins[m.group(1)] = reg_origins.get(src, "reg")
                break

        m = _RE_LOAD_SP.search(clean)
        if m:
            off_hex = hex(_parse_offset(m.group(3)))
            reg_map[m.group(2)]     = f"sp_{off_hex}"
            reg_origins[m.group(2)] = "fp"

        m = _RE_LOAD_NONSP.search(clean)
        if m:
            reg_origins[m.group(1)] = "fp"

        m_c16 = re.search(r'\blw\s+\$(\w+),\s*%call16\((\w+)\)\(\$gp\)', clean)
        if m_c16:
            reg_map[m_c16.group(1)]     = m_c16.group(2)
            reg_origins[m_c16.group(1)] = "symbol"

        m_got = re.search(r'\blw\s+\$(\w+),\s*%got\((\w+)(?:\s*+\s*\S+)?\)\(\$gp\)', clean)
        if m_got:
            sym = m_got.group(2)
            reg = m_got.group(1)
            reg_map[reg]     = sym
            reg_origins[reg] = "symbol"
            if sym in env_sym:
                curr["_global_rw"].setdefault(sym, set()).add("r")

        m_gpr = re.search(r'\b(?:lw|lh|lb|lhu|lbu|ld|lwc1|ldc1|sw|sh|sb|sd|swc1|sdc1)\s+'
                          r'\$(\w+),\s*%gp_rel\((\w+)\)\(\$gp\)', clean)
        if m_gpr:
            reg_map[m_gpr.group(1)]     = m_gpr.group(2)
            reg_origins[m_gpr.group(1)] = "symbol"

        curr["_reg_origins"] = dict(reg_origins)

        m_gwrw = _RE_MEM.search(clean)
        if m_gwrw:
            gop, _, _goff, gbase = m_gwrw.groups()
            gbase_name = reg_map.get(gbase, gbase)
            if gbase_name in env_sym and gbase != "sp":
                grw = "r" if gop.lower().startswith("l") else "w"
                curr["_global_rw"].setdefault(gbase_name, set()).add(grw)

        m_gps = re.search(
            r'\b(lw|lh|lb|lhu|lbu|ld|lwc1|ldc1|sw|sh|sb|sd|swc1|sdc1)\s+'
            r'\$\w+,\s*%gp_rel\((\w+)\)\(\$gp\)', clean)
        if m_gps:
            sym_name = m_gps.group(2)
            if sym_name in env_sym:
                grw = "r" if m_gps.group(1).startswith("l") else "w"
                curr["_global_rw"].setdefault(sym_name, set()).add(grw)

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
            argc = 0
            for i, ar in enumerate(['a0','a1','a2','a3']):
                if ar in curr["_call_args_set"]:
                    argc = i + 1
            ctx = {ar: desc for ar, desc in curr["_call_arg_desc"].items()
                   if desc is not None and ar in curr["_call_args_set"]}
            entry = {"type": "direct", "name": m.group(1), "argc": argc}
            if ctx:
                entry["ctx"] = ctx
            curr["calls"].append(entry)
            curr["_call_args_set"] = set()
            curr["_call_arg_desc"] = {}

        m = _RE_JALR.search(clean)
        if m:
            reg  = m.group(1)
            argc = 0
            for i, ar in enumerate(['a0','a1','a2','a3']):
                if ar in curr["_call_args_set"]:
                    argc = i + 1
            ctx = {ar: desc for ar, desc in curr["_call_arg_desc"].items()
                   if desc is not None and ar in curr["_call_args_set"]}

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
            curr["_call_args_set"] = set()
            curr["_call_arg_desc"] = {}

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

        m_farg = re.search(
            r'\b(swc1|sdc1)\s+\$(f12|f14),\s*(?:0x[\da-fA-F]+|-?\d+)\(\$sp\)', clean)
        if m_farg:
            freg = m_farg.group(2)
            if freg == "f12":
                curr["_arg_read"].add("a0")
                if m_farg.group(1) == "sdc1":
                    curr["_arg_read"].add("a1")
            elif freg == "f14":
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
                if re.search(r'\$f12\b', clean):
                    curr["_arg_read"].add("a0")
                    if is_double:
                        curr["_arg_read"].add("a1")
                if re.search(r'\$f14\b', clean):
                    curr["_arg_read"].add("a2")
                    if is_double:
                        curr["_arg_read"].add("a3")

        m_fmov = re.search(r'\bmov\.[sd]\s+\$f\w+,\s+\$(f12|f14)\b', clean)
        if m_fmov:
            freg = m_fmov.group(1)
            is_double_mov = clean.lstrip().startswith("mov.d")
            if freg == "f12":
                curr["_arg_read"].add("a0")
                if is_double_mov:
                    curr["_arg_read"].add("a1")
            elif freg == "f14":
                curr["_arg_read"].add("a2")
                if is_double_mov:
                    curr["_arg_read"].add("a3")

        m_mtc1 = re.search(r'\bmtc1\s+\$(a[0-3]),\s*\$f\w+\b', clean)
        if m_mtc1:
            curr["_arg_read"].add(m_mtc1.group(1))

        m_argsrc = re.search(
            r'\b(?:move|addu|addiu|subu|and|andi|or|ori|xor|xori|nor|'
            r'sll|srl|sra|sllv|srlv|srav|slt|sltu|slti|sltiu)\s+'
            r'\$(?!a[0-3]\b)(\w+),\s*\$(a[0-3])\b', clean)
        if m_argsrc:
            arg = m_argsrc.group(2)
            if arg not in curr["_arg_written"]:
                curr["_arg_read"].add(arg)

        for arg in ARG_REGS:
            if arg not in curr["_arg_written"]:
                m_sw = re.search(
                    r'\bsw\s+\$' + arg + r',\s*(0x[\da-fA-F]+|\d+)\(\$sp\)',
                    clean)
                if m_sw:
                    off = _parse_offset(m_sw.group(1))
                    curr["_arg_save_slots"][off] = arg
                    curr["_arg_save_only"].add(arg)

        for arg in ARG_REGS:
            if arg in curr["_arg_written"]:
                continue
            if re.search(r'\$' + arg + r'\b', clean):
                wm = _RE_ARG_WRITE.search(clean)
                if wm and wm.group(1) == arg:
                    if len(re.findall(r'\$' + arg + r'\b', clean)) > 1:
                        curr["_arg_read"].add(arg)
                        curr["_arg_save_only"].discard(arg)
                    else:
                        curr["_arg_written"].add(arg)
                else:
                    curr["_arg_read"].add(arg)
                    curr["_arg_save_only"].discard(arg)

        if _RE_V0_WRITE.search(clean):
            curr["_v0_written"] = True

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

    if curr is not None:
        _flush(curr, funcs, env_s, env_ext)

    return funcs


# ---------------------------------------------------------------------------
# 4. WORKER & ORCHESTRATION
# ---------------------------------------------------------------------------

def worker(args):
    s_path, h_path, h_dir, out_path = args
    try:
        s_map, sym_map, ext_map, enum_map = parse_env_expert(h_path, h_dir)
        funcs = analyze_asm_semantic(s_path, s_map, sym_map, ext_map)
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
    print("--- Semantic Expert Extractor ---")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if not os.path.exists(INPUT_ASM):
        print(f"[ERROR] ASM directory not found: {INPUT_ASM}")
        return
    if not os.path.exists(INPUT_HEADER):
        print(f"[ERROR] Header directory not found: {INPUT_HEADER}")
        return

    tasks = []
    for fname in sorted(os.listdir(INPUT_ASM)):
        if not fname.endswith(".s"):
            continue
        base = fname[:-2]
        s_path = os.path.join(INPUT_ASM, fname)
        h_path = os.path.join(INPUT_HEADER, f"{base}.h")
        out_path = os.path.join(OUTPUT_DIR, f"{base}.json")
        tasks.append((s_path, h_path, INPUT_HEADER, out_path))

    if not tasks:
        print("[SKIP] No .s files found.")
        return

    print(f"Found {len(tasks)} assembly files to process.")
    errors = []
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool() as pool:
        for ok, err in tqdm(pool.imap_unordered(worker, tasks),
                            total=len(tasks), desc="Processing"):
            if not ok and err:
                errors.append(err)

    if errors:
        print(f"\n[{len(errors)} errors]")
        for e in errors[:10]:
            print(f"  {e}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")
    else:
        print(f"\n[OK] All {len(tasks)} files processed successfully.")


if __name__ == "__main__":
    main()
