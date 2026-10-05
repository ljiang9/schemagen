#!/usr/bin/env python3
"""schemagen — 从 JSON 样本推断 schema，生成 JSON Schema / TypeScript / Pydantic。

纯标准库，纯本地：把 API 返回或日志里的 JSON 样例丢进来，
一次拿到三种语言的 schema 草稿，再人工微调。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime

VERSION = "0.1.0"

ENUM_MAX_VALUES = 8      # 不同字符串值不超过这么多个就判为 enum
ENUM_MIN_VALUES = 2      # 至少两个不同值才判 enum（避免单值锁死）
ENUM_SAMPLE_CAP = 200    # 每字段最多记多少个样本值用于 enum 判断


# ---------------------------------------------------------------------------
# 类型推断：把多个样本合并成一棵 Shape 树
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"                       # 日期部分
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"  # 可选时间部分
    r"(?:Z|[+-]\d{2}:?\d{2})?)?$"               # 可选时区
)


def _looks_datetime(s: str) -> bool:
    if not _ISO_RE.match(s):
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _kind_of(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):      # 必须在 int 之前：bool 是 int 的子类
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"  # 兜底：未知类型按字符串处理


class Shape:
    """一个 JSON 位置在多个样本中观察到的类型汇总。"""

    def __init__(self):
        self.seen = 0            # 出现在多少个样本里（仅对象字段有意义）
        self.kinds = set()       # {"object","array","string","int","float","bool","null"}
        self.is_datetime = False # feed 结束后由 finalize() 统一计算
        self.fields = {}         # object: name -> Shape
        self.elem = None         # array: 元素 Shape
        self.str_values = []     # string: 收集到的样本值（上限 ENUM_SAMPLE_CAP）

    def feed(self, value):
        self.seen += 1
        kind = _kind_of(value)
        self.kinds.add(kind)
        if kind == "object":
            for k, v in value.items():
                self.fields.setdefault(k, Shape()).feed(v)
        elif kind == "array":
            if self.elem is None:
                self.elem = Shape()
            for v in value:
                self.elem.feed(v)
        elif kind == "string":
            if len(self.str_values) < ENUM_SAMPLE_CAP:
                self.str_values.append(value)
        return self

    def finalize(self):
        """递归收尾：计算 is_datetime（字符串须全部为 ISO 8601）。"""
        if "string" in self.kinds and self.str_values:
            self.is_datetime = all(_looks_datetime(v) for v in self.str_values)
        for child in self.fields.values():
            child.finalize()
        if self.elem is not None:
            self.elem.finalize()
        return self

    # -- 派生属性 ---------------------------------------------------------
    @property
    def nullable(self) -> bool:
        return "null" in self.kinds

    @property
    def scalar_kinds(self):
        return [k for k in ("string", "int", "float", "bool") if k in self.kinds]

    @property
    def enum_values(self):
        """符合 enum 条件时返回排序后的值列表，否则 None。

        规则：不同值 2~8 个，且至少有一个值重复出现过。
        重复是"封闭标签集"的信号——只出现一次的 ID 类字符串
        （如订单号）不应被锁成 enum。
        """
        if "string" not in self.kinds or self.is_datetime:
            return None
        distinct = sorted(set(self.str_values))
        if not (ENUM_MIN_VALUES <= len(distinct) <= ENUM_MAX_VALUES):
            return None
        if max(Counter(self.str_values).values(), default=0) < 2:
            return None
        return distinct


# ---------------------------------------------------------------------------
# 输入读取：.json（单个对象/数组）与 .jsonl（每行一个样本）
# ---------------------------------------------------------------------------

def load_samples(path: str):
    """返回 (samples: list, source_note: str)。"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        raise ValueError("输入文件为空")
    if path.endswith(".jsonl"):
        samples, bad = [], 0
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError as e:
                bad += 1
                print(f"警告：第 {i} 行解析失败，已跳过（{e.msg}）", file=sys.stderr)
        if not samples:
            raise ValueError("JSONL 里没有可解析的行")
        if bad:
            print(f"警告：共跳过 {bad} 行坏数据", file=sys.stderr)
        return samples, f"JSONL，共 {len(samples)} 个样本"
    # .json
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败：{e.msg}（第 {e.lineno} 行）")
    if isinstance(doc, list):
        if not doc:
            raise ValueError("JSON 数组为空，没有可推断的样本")
        return doc, f"JSON 数组，共 {len(doc)} 个样本"
    return [doc], "单个 JSON 对象"


def build_shape(samples) -> Shape:
    root = Shape()
    for s in samples:
        root.feed(s)
    return root.finalize()


# ---------------------------------------------------------------------------
# 命名工具
# ---------------------------------------------------------------------------

def _pascal(name: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", name)
    out = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return out or "Item"


# ---------------------------------------------------------------------------
# Emitter 1: JSON Schema (draft 2020-12)
# ---------------------------------------------------------------------------

def emit_jsonschema(root: Shape, name: str, strict: bool) -> str:
    total = root.seen

    def schema_of(shape: Shape, field_seen_total: int):
        kinds = shape.kinds - {"null"}
        # 对象
        if kinds == {"object"}:
            props, required = {}, []
            for fname, fshape in shape.fields.items():
                props[fname] = schema_of(fshape, fshape.seen)
                if strict or fshape.seen >= total:
                    required.append(fname)
            node = {"type": "object", "properties": props}
            if required:
                node["required"] = sorted(required)
            if shape.nullable:
                node["type"] = ["object", "null"]
            return node
        # 数组
        if kinds == {"array"}:
            items = {"type": "object", "properties": {},
                     "additionalProperties": True} if shape.elem is None \
                else schema_of(shape.elem, shape.elem.seen)
            node = {"type": "array", "items": items}
            if shape.nullable:
                node["type"] = ["array", "null"]
            return node
        # 标量（可能混合）
        scalars = shape.scalar_kinds
        if not scalars:
            return {"type": "null"} if shape.nullable else {}
        if len(scalars) == 1:
            t = scalars[0]
            node = {"type": "integer" if t == "int" else
                    "number" if t == "float" else t}
            if t == "string" and shape.is_datetime:
                node["format"] = "date-time"
            enum = shape.enum_values
            if enum is not None:
                node["enum"] = enum
            if shape.nullable:
                node["type"] = [node["type"], "null"]
            return node
        # 混合标量：用 anyOf
        any_of = []
        for t in scalars:
            sub = {"type": "integer" if t == "int" else
                   "number" if t == "float" else t}
            if t == "string" and shape.is_datetime:
                sub["format"] = "date-time"
            any_of.append(sub)
        node = {"anyOf": any_of}
        if shape.nullable:
            node["anyOf"].append({"type": "null"})
        return node

    doc = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": name,
    }
    doc.update(schema_of(root, total))
    return json.dumps(doc, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Emitter 2: TypeScript interfaces
# ---------------------------------------------------------------------------

def emit_typescript(root: Shape, name: str, strict: bool) -> str:
    total = root.seen
    blocks = []
    used = set()

    def unique(base: str) -> str:
        cand, i = base, 2
        while cand in used:
            cand = f"{base}{i}"
            i += 1
        used.add(cand)
        return cand

    def ts_type(shape: Shape, hint: str, required: bool) -> str:
        kinds = shape.kinds - {"null"}
        if kinds == {"object"}:
            iname = unique(_pascal(hint))
            lines = [f"export interface {iname} {{"]
            for fname, fshape in shape.fields.items():
                req = strict or fshape.seen >= total
                opt = "" if req else "?"
                ftype = ts_type(fshape, f"{iname}{_pascal(fname)}", req)
                lines.append(f"  {fname}{opt}: {ftype};")
            lines.append("}")
            blocks.append("\n".join(lines))
            base = iname
        elif kinds == {"array"}:
            et = ts_type(shape.elem, hint + "Item", True) if shape.elem else "unknown"
            base = f"({et})[]" if " | " in et else f"{et}[]"
        else:
            scalars = shape.scalar_kinds
            mapping = {"string": "string", "int": "number", "float": "number",
                       "bool": "boolean"}
            if not scalars:
                base = "null"
            elif len(scalars) == 1:
                t = scalars[0]
                if t == "string" and not shape.is_datetime:
                    enum = shape.enum_values
                    base = " | ".join(json.dumps(v) for v in enum) if enum else "string"
                else:
                    base = mapping[t]
            else:
                base = " | ".join(mapping[t] for t in scalars)
        if shape.nullable and base != "null":
            base = f"{base} | null"
        return base

    if (root.kinds - {"null"}) == {"object"}:
        iname = unique(_pascal(name))
        lines = [f"export interface {iname} {{"]
        for fname, fshape in root.fields.items():
            req = strict or fshape.seen >= total
            opt = "" if req else "?"
            ftype = ts_type(fshape, f"{iname}{_pascal(fname)}", req)
            lines.append(f"  {fname}{opt}: {ftype};")
        lines.append("}")
        blocks.append("\n".join(lines))
    else:
        alias = ts_type(root, name, True)
        blocks.append(f"export type {_pascal(name)} = {alias};")

    header = "// 由 schemagen 生成的草稿，请人工复核后再用。\n"
    return header + "\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# Emitter 3: Pydantic v2 models
# ---------------------------------------------------------------------------

def emit_pydantic(root: Shape, name: str, strict: bool) -> str:
    total = root.seen
    blocks = []
    used = set()

    def unique(base: str) -> str:
        cand, i = base, 2
        while cand in used:
            cand = f"{base}{i}"
            i += 1
        used.add(cand)
        return cand

    def py_type(shape: Shape, hint: str) -> str:
        kinds = shape.kinds - {"null"}
        if kinds == {"object"}:
            cname = unique(_pascal(hint))
            blocks.append(model_block(cname, shape.fields, cname))
            base = cname
        elif kinds == {"array"}:
            et = py_type(shape.elem, hint + "Item") if shape.elem else "Any"
            base = f"List[{et}]"
        else:
            scalars = shape.scalar_kinds
            mapping = {"string": "str", "int": "int", "float": "float",
                       "bool": "bool"}
            if not scalars:
                base = "None"
            elif len(scalars) == 1:
                t = scalars[0]
                if t == "string" and not shape.is_datetime:
                    enum = shape.enum_values
                    base = ("Literal[" + ", ".join(repr(v) for v in enum) + "]"
                            if enum else "str")
                else:
                    base = mapping[t]
            else:
                base = "Union[" + ", ".join(mapping[t] for t in scalars) + "]"
        if shape.nullable and base != "None":
            base = f"Optional[{base}]"
        return base

    def field_line(fname: str, fshape: Shape, hint: str) -> str:
        ftype = py_type(fshape, hint)
        ident_ok = bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", fname))
        target = fname if ident_ok else f"field_{abs(hash(fname)) % 10000}"
        if fshape.kinds == {"null"}:
            # 样本中恒为 null：真实类型未知，诚实地标 Any + TODO
            ann, default = "Any", " = None  # TODO: 样本中恒为 null，类型未知"
        else:
            req = strict or fshape.seen >= total
            if req and not fshape.nullable:
                ann, default = ftype, ""
            elif ftype.startswith("Optional["):
                ann, default = ftype, " = None"  # py_type 已包过 Optional，不再重复
            else:
                ann, default = f"Optional[{ftype}]", " = None"
        if ident_ok:
            return f"    {target}: {ann}{default}"
        alias = f"Field(alias={fname!r})" if default == "" \
            else f"Field(default=None, alias={fname!r})"
        return (f"    # TODO: {fname!r} 不是合法 Python 标识符，已用 alias\n"
                f"    {target}: {ann} = {alias}")

    def model_block(cname: str, fields: dict, hint: str) -> str:
        lines = [f"class {cname}(BaseModel):"]
        if not fields:
            lines.append("    pass")
        for fname, fshape in fields.items():
            lines.append(field_line(fname, fshape, f"{cname}{_pascal(fname)}"))
        return "\n".join(lines)

    if (root.kinds - {"null"}) == {"object"}:
        cname = unique(_pascal(name))
        blocks.append(model_block(cname, root.fields, cname))
    else:
        alias = py_type(root, name)
        blocks.append(f"{_pascal(name)} = {alias}")

    header = ('"""由 schemagen 生成的草稿，请人工复核后再用。"""\n'
              "from typing import Any, List, Literal, Optional, Union\n\n"
              "from pydantic import BaseModel, Field\n\n\n")
    return header + "\n\n\n".join(blocks) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="schemagen",
        description="从 JSON / JSONL 样本推断 schema，生成 JSON Schema / TypeScript / Pydantic 草稿（纯本地）。")
    ap.add_argument("input", help="样本文件：.json（对象或数组）或 .jsonl（每行一个样本）")
    ap.add_argument("--jsonschema", action="store_true", help="只输出 JSON Schema")
    ap.add_argument("--ts", action="store_true", help="只输出 TypeScript")
    ap.add_argument("--pydantic", action="store_true", help="只输出 Pydantic")
    ap.add_argument("--name", default="Root", help="根类型名（默认 Root）")
    ap.add_argument("--strict", action="store_true", help="所有字段都标为必填（默认：只在全部样本都出现的字段必填）")
    ap.add_argument("-o", "--outdir", help="把三种输出分别写入目录（schema.json / types.ts / models.py）")
    ap.add_argument("--version", action="version", version=f"schemagen {VERSION}")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.input):
        print(f"error: 找不到输入文件：{args.input}", file=sys.stderr)
        return 1
    try:
        samples, note = load_samples(args.input)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    root = build_shape(samples)
    want = {"jsonschema": args.jsonschema, "ts": args.ts, "pydantic": args.pydantic}
    if not any(want.values()):
        want = {k: True for k in want}  # 默认三种都出

    outputs = {}
    if want["jsonschema"]:
        outputs["schema.json"] = emit_jsonschema(root, args.name, args.strict)
    if want["ts"]:
        outputs["types.ts"] = emit_typescript(root, args.name, args.strict)
    if want["pydantic"]:
        outputs["models.py"] = emit_pydantic(root, args.name, args.strict)

    print(f"# 输入：{args.input}（{note}）\n", file=sys.stderr)
    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)
        for fname, text in outputs.items():
            p = os.path.join(args.outdir, fname)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"已写入 {p}", file=sys.stderr)
        return 0

    titles = {"schema.json": "## JSON Schema", "types.ts": "## TypeScript",
              "models.py": "## Pydantic"}
    for fname, text in outputs.items():
        print(titles[fname])
        print("```" + ("json" if fname == "schema.json"
                        else "ts" if fname == "types.ts" else "python"))
        print(text.rstrip())
        print("```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
