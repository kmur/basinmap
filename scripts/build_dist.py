#!/usr/bin/env python3
"""Build ./dist for Cloudflare Workers Static Assets and local dev/CI execution.

Compiles src/frontend/*.ts to ES2022 browser modules (using `tsc` when available,
or the built-in zero-dependency TypeScript stripper in pure Python), copies static
HTML/CSS, and stages public-r2/releases and public-r2/overlays into ./dist.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Set, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT_DIR / "src" / "frontend"
PUBLIC_R2_DIR = ROOT_DIR / "public-r2"
DIST_DIR = ROOT_DIR / "dist"
JSC_BIN = Path("/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc")


def _skip_string(src: str, i: int) -> int:
    quote = src[i]
    n = len(src)
    i += 1
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if quote == "`" and ch == "$" and i + 1 < n and src[i + 1] == "{":
            # Keep balanced ${...} skipping simple nested braces inside template literals
            i += 2
            depth = 1
            while i < n and depth > 0:
                if src[i] in ("'", '"', "`"):
                    i = _skip_string(src, i)
                elif src[i] == "{":
                    depth += 1
                    i += 1
                elif src[i] == "}":
                    depth -= 1
                    i += 1
                else:
                    i += 1
            continue
        if ch == quote:
            return i + 1
        i += 1
    return n


def _skip_regex(src: str, i: int) -> int:
    n = len(src)
    i += 1
    in_class = False
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]" and in_class:
            in_class = False
        elif ch == "/" and not in_class:
            i += 1
            while i < n and src[i].isalpha():
                i += 1
            return i
        elif ch == "\n":
            return i
        i += 1
    return n


def _prev_non_ws_char(chunks: List[str]) -> str:
    for part in reversed(chunks):
        for ch in reversed(part):
            if ch not in (" ", "\t", "\r", "\n"):
                return ch
    return ""


def _skip_comment(src: str, i: int) -> int:
    n = len(src)
    if src[i : i + 2] == "//":
        nl = src.find("\n", i + 2)
        return n if nl == -1 else nl
    if src[i : i + 2] == "/*":
        end = src.find("*/", i + 2)
        return n if end == -1 else end + 2
    return i


def _strip_top_level_interfaces(src: str, type_names: Set[str]) -> str:
    out: List[str] = []
    i = 0
    n = len(src)
    while i < n:
        if src[i] in ("'", '"', "`"):
            nxt = _skip_string(src, i)
            out.append(src[i:nxt])
            i = nxt
            continue
        if src[i : i + 2] in ("//", "/*"):
            nxt = _skip_comment(src, i)
            out.append(src[i:nxt])
            i = nxt
            continue

        if src[i] == "/" and _prev_non_ws_char(out) in ("", "(", ",", "=", ":", "[", "!", "&", "|", "?", ";", "{", "}"):
            nxt = _skip_regex(src, i)
            out.append(src[i:nxt])
            i = nxt
            continue

        m = re.match(r"(?:export\s+)?interface\s+([A-Za-z_]\w*)\b", src[i:])
        at_line_start = i == 0 or src[i - 1] == "\n"
        if m and at_line_start:
            type_names.add(m.group(1))
            brace = src.find("{", i + m.end())
            if brace != -1:
                depth = 1
                j = brace + 1
                while j < n and depth > 0:
                    if src[j] in ("'", '"', "`"):
                        j = _skip_string(src, j)
                    elif src[j : j + 2] in ("//", "/*"):
                        j = _skip_comment(src, j)
                    elif src[j] == "{":
                        depth += 1
                        j += 1
                    elif src[j] == "}":
                        depth -= 1
                        j += 1
                    else:
                        j += 1
                while j < n and src[j] in (" ", "\t", "\r", "\n"):
                    j += 1
                i = j
                continue

        out.append(src[i])
        i += 1
    return "".join(out)


def _filter_imports(src: str, known_types: Set[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        raw_items = [item.strip() for item in match.group(1).split(",") if item.strip()]
        kept = [item for item in raw_items if item not in known_types and not item.startswith("type ")]
        if not kept:
            return ""
        return f"import {{\n  " + ",\n  ".join(kept) + f",\n}} from {match.group(2)};"

    return re.sub(r"import\s*\{([^}]*)\}\s*from\s*(['\"][^'\"]+['\"]);?", repl, src, flags=re.DOTALL)


def _find_matching_brace(src: str, open_pos: int) -> int:
    n = len(src)
    depth = 1
    j = open_pos + 1
    while j < n and depth > 0:
        if src[j] in ("'", '"', "`"):
            j = _skip_string(src, j)
        elif src[j : j + 2] in ("//", "/*"):
            j = _skip_comment(src, j)
        elif src[j] == "{":
            depth += 1
            j += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return j
            j += 1
        else:
            j += 1
    return n - 1


def _consume_type_expr(src: str, pos: int, stop_chars: Set[str]) -> int:
    n = len(src)
    paren = 0
    brace = 0
    bracket = 0
    angle = 0
    while pos < n:
        ch = src[pos]
        if ch in ("'", '"', "`"):
            pos = _skip_string(src, pos)
            continue
        if paren == 0 and brace == 0 and bracket == 0 and angle == 0 and ch in stop_chars:
            if ch == "{" and "{" in stop_chars:
                close_b = _find_matching_brace(src, pos)
                k = close_b + 1
                while k < n and src[k] in (" ", "\t", "\r", "\n"):
                    k += 1
                if k < n and src[k] in ("{", "|", "&", "["):
                    # This `{ ... }` is a TS inline object type, not the function body.
                    pos = close_b + 1
                    continue
                return pos
            if ch == "=" and pos + 1 < n and src[pos + 1] == ">" and "=>" not in stop_chars:
                pass
            return pos
        if ch in ("(", "{", "["):
            if ch == "(":
                paren += 1
            elif ch == "{":
                brace += 1
            else:
                bracket += 1
            pos += 1
            continue
        if ch == ")" and paren > 0:
            paren -= 1
            pos += 1
            continue
        if ch == "}" and brace > 0:
            brace -= 1
            pos += 1
            continue
        if ch == "]" and bracket > 0:
            bracket -= 1
            pos += 1
            continue
        if ch == "<":
            angle += 1
            pos += 1
            continue
        if ch == ">" and angle > 0:
            # Skip `=>` arrow token without miscounting `>` as closing angle bracket
            if pos > 0 and src[pos - 1] == "=":
                pos += 1
                continue
            angle -= 1
            pos += 1
            continue
        pos += 1
    return pos


def _strip_param_list_types(params_raw: str) -> str:
    out: List[str] = []
    i = 0
    n = len(params_raw)
    while i < n:
        ch = params_raw[i]
        if ch in ("'", '"', "`"):
            nxt = _skip_string(params_raw, i)
            out.append(params_raw[i:nxt])
            i = nxt
            continue
        if ch == "?" and params_raw[i + 1 : i + 2] == ":":
            # Optional parameter marker (`name?: T`); the type is stripped on the next pass.
            i += 1
            continue
        if ch == ":":
            end = _consume_type_expr(params_raw, i + 1, {",", "="})
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_inline_ts(src: str) -> str:
    out: List[str] = []
    i = 0
    n = len(src)

    while i < n:
        ch = src[i]
        if ch in ("'", '"', "`"):
            # Inside template literal, recursively strip `${ ... }` expressions!
            if ch == "`":
                out.append("`")
                i += 1
                while i < n:
                    if src[i] == "\\":
                        out.append(src[i : i + 2])
                        i += 2
                    elif src[i : i + 2] == "${":
                        out.append("${")
                        i += 2
                        depth = 1
                        expr_start = i
                        while i < n and depth > 0:
                            if src[i] in ("'", '"', "`"):
                                i = _skip_string(src, i)
                            elif src[i] == "{":
                                depth += 1
                                i += 1
                            elif src[i] == "}":
                                depth -= 1
                                if depth == 0:
                                    break
                                i += 1
                            else:
                                i += 1
                        out.append(_strip_inline_ts(src[expr_start:i]))
                        out.append("}")
                        i += 1
                    elif src[i] == "`":
                        out.append("`")
                        i += 1
                        break
                    else:
                        out.append(src[i])
                        i += 1
                continue
            nxt = _skip_string(src, i)
            out.append(src[i:nxt])
            i = nxt
            continue

        if src[i : i + 2] in ("//", "/*"):
            nxt = _skip_comment(src, i)
            out.append(src[i:nxt])
            i = nxt
            continue

        if ch == "/" and _prev_non_ws_char(out) in ("", "(", ",", "=", ":", "[", "!", "&", "|", "?", ";", "{", "}"):
            nxt = _skip_regex(src, i)
            out.append(src[i:nxt])
            i = nxt
            continue

        # Function declaration: [export ][async ]function name<T>(params): ReturnType {
        m_fn = re.match(
            r"((?:export\s+)?(?:async\s+)?function\s+[A-Za-z_]\w*)\s*(?:<[^>]+>)?\s*\(",
            src[i:],
        )
        if m_fn and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            out.append(m_fn.group(1) + "(")
            p_start = i + m_fn.end()
            p_end = p_start
            p_depth = 1
            while p_end < n and p_depth > 0:
                if src[p_end] in ("'", '"', "`"):
                    p_end = _skip_string(src, p_end)
                elif src[p_end] == "(":
                    p_depth += 1
                    p_end += 1
                elif src[p_end] == ")":
                    p_depth -= 1
                    if p_depth == 0:
                        break
                    p_end += 1
                else:
                    p_end += 1
            out.append(_strip_param_list_types(src[p_start:p_end]) + ")")
            i = p_end + 1
            # Optional `: ReturnType` before `{`
            ws = i
            while ws < n and src[ws] in (" ", "\t"):
                ws += 1
            if ws < n and src[ws] == ":":
                body_pos = _consume_type_expr(src, ws + 1, {"{"})
                out.append(" ")
                i = body_pos
            continue

        # Typed catch (err: any)
        m_catch = re.match(r"catch\s*\(\s*([A-Za-z_]\w*)\s*:\s*[A-Za-z_][\w<>\[\] |]*\)", src[i:])
        if m_catch and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            out.append(f"catch ({m_catch.group(1)})")
            i += m_catch.end()
            continue

        # Variable declaration with type annotation: (const|let|var) ident: Type =
        m_var = re.match(r"((?:const|let|var)\s+[A-Za-z_]\w*)\s*:\s*", src[i:])
        if m_var and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            eq_pos = _consume_type_expr(src, i + m_var.end(), {"=", ";", "\n"})
            if eq_pos < n and src[eq_pos] == "=":
                out.append(m_var.group(1) + " ")
                i = eq_pos
                continue

        # Arrow function parameter list: (a: T, b: U)[: R] =>
        if ch == "(":
            p_end = i + 1
            p_depth = 1
            while p_end < n and p_depth > 0:
                if src[p_end] in ("'", '"', "`"):
                    p_end = _skip_string(src, p_end)
                elif src[p_end] == "(":
                    p_depth += 1
                    p_end += 1
                elif src[p_end] == ")":
                    p_depth -= 1
                    if p_depth == 0:
                        break
                    p_end += 1
                else:
                    p_end += 1
            after_paren = p_end + 1
            # Check if this parenthesized group is followed by `[: ReturnType] =>`
            ws = after_paren
            while ws < n and src[ws] in (" ", "\t"):
                ws += 1
            is_arrow = False
            arrow_pos = ws
            if ws < n and src[ws] == ":":
                cand = _consume_type_expr(src, ws + 1, {"="})
                if src[cand : cand + 2] == "=>":
                    is_arrow = True
                    arrow_pos = cand
            elif src[ws : ws + 2] == "=>":
                is_arrow = True
                arrow_pos = ws

            if is_arrow:
                inner = _strip_inline_ts(_strip_param_list_types(src[i + 1 : p_end]))
                out.append(f"({inner}) ")
                i = arrow_pos
                continue

        # Generic constructor call: new Set<string>(...) / new Map<K, V>(...)
        m_gen = re.match(r"(new\s+(?:Set|Map|Array|Promise))<[^>]+>\s*\(", src[i:])
        if m_gen and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
            out.append(m_gen.group(1) + "(")
            i += m_gen.end()
            continue

        # Type assertions: ` as unknown as Foo` or ` as Foo[][]`
        m_as = re.match(
            r"\s+as\s+(?:unknown\s+as\s+)?(?:any|unknown|never|[A-Za-z_]\w*(?:<[^>]+>)?)(?:\[\])*",
            src[i:],
        )
        if m_as and (i == 0 or src[i - 1] in (")", "]", "}", " ", "\t") or src[i - 1].isalnum() or src[i - 1] == "_"):
            # Ensure we don't match inside an identifier like `has_...`
            after_idx = i + m_as.end()
            if after_idx >= n or not (src[after_idx].isalnum() or src[after_idx] == "_"):
                i = after_idx
                continue

        out.append(ch)
        i += 1

    return "".join(out)


def compile_ts_module(src: str, known_types: Set[str]) -> str:
    step1 = _strip_top_level_interfaces(src, known_types)
    step2 = _filter_imports(step1, known_types)
    return _strip_inline_ts(step2).lstrip("\n")


def verify_js_module(js_path: Path) -> None:
    """Syntax-check an emitted ES module with JavaScriptCore (macOS) and/or Node.js when available."""
    if JSC_BIN.exists():
        subprocess.run([str(JSC_BIN), "-m", str(js_path)], check=True)
    node = shutil.which("node")
    if node:
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / (js_path.stem + ".mjs")
            probe.write_text(js_path.read_text(encoding="utf-8"), encoding="utf-8")
            result = subprocess.run([node, "--check", str(probe)], capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"Emitted {js_path.name} is not valid JavaScript:\n{result.stderr}")


def build_dist() -> Dict[str, Path]:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    assets_dir = DIST_DIR / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    known_types: Set[str] = set()
    types_ts = (FRONTEND_DIR / "types.ts").read_text(encoding="utf-8")
    types_js = compile_ts_module(types_ts, known_types)
    types_out = assets_dir / "types.js"
    types_out.write_text(types_js, encoding="utf-8")

    app_ts = (FRONTEND_DIR / "app.ts").read_text(encoding="utf-8")
    app_js = compile_ts_module(app_ts, known_types)
    app_out = assets_dir / "app.js"
    app_out.write_text(app_js, encoding="utf-8")

    verify_js_module(types_out)
    verify_js_module(app_out)

    index_out = DIST_DIR / "index.html"
    shutil.copy2(FRONTEND_DIR / "index.html", index_out)
    styles_out = assets_dir / "styles.css"
    shutil.copy2(FRONTEND_DIR / "styles.css", styles_out)

    # Stage public-r2 releases and overlays into ./dist for local static preview & wrangler dev.
    for sub in ("releases", "overlays"):
        src_sub = PUBLIC_R2_DIR / sub
        if src_sub.exists():
            shutil.copytree(src_sub, DIST_DIR / sub, dirs_exist_ok=True)

    return {
        "index": index_out,
        "styles": styles_out,
        "types_js": types_out,
        "app_js": app_out,
    }


if __name__ == "__main__":
    built = build_dist()
    print(f"Built frontend bundle into {DIST_DIR}: " + ", ".join(p.name for p in built.values()))
