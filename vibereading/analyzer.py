"""Read-only, conservative source indexing for VibeReading.

Tree-sitter supplies syntax, not runtime name resolution.  Only local bindings or
explicit Python imports become resolved edges; uncertain receiver/package calls
are candidates or remain unresolved.  Inspected source is never imported.
"""
from __future__ import annotations

import ast
import hashlib
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from tree_sitter_language_pack import get_parser

SUPPORTED_LANGUAGES = ["Python", "R", "Java", "C", "C++", "Rust", "JavaScript", "TypeScript", "Go"]
EXTENSIONS = {
    ".py": ("Python", "python"), ".r": ("R", "r"),
    ".java": ("Java", "java"), ".c": ("C", "c"), ".h": ("C", "c"),
    ".cpp": ("C++", "cpp"), ".cc": ("C++", "cpp"), ".cxx": ("C++", "cpp"),
    ".hpp": ("C++", "cpp"), ".hh": ("C++", "cpp"), ".hxx": ("C++", "cpp"),
    ".rs": ("Rust", "rust"), ".js": ("JavaScript", "javascript"),
    ".jsx": ("JavaScript", "javascript"), ".mjs": ("JavaScript", "javascript"),
    ".cjs": ("JavaScript", "javascript"), ".ts": ("TypeScript", "typescript"),
    ".tsx": ("TypeScript", "tsx"), ".go": ("Go", "go"),
}
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "target", "build", "dist", ".vibereading", "work"}
MAX_FILES = 1500
MAX_FILE_BYTES = 1024 * 1024
FUNCTION_TYPES = {"function_definition", "function_declaration", "function_item", "method_declaration", "method_definition", "constructor_declaration", "generator_function_declaration", "generator_function", "function_expression", "arrow_function"}
CLASS_TYPES = {"class_definition", "class_declaration", "class_specifier", "struct_specifier", "interface_declaration", "enum_declaration", "record_declaration", "trait_item", "impl_item", "object_definition"}
MODULE_TYPES = {"namespace_definition", "mod_item", "internal_module"}
CALL_TYPES = {"call", "call_expression", "method_invocation"}


def _text(node, data: bytes) -> str:
    """Tree-sitter coordinates are UTF-8 bytes; slice bytes before decoding."""
    return data[node.start_byte:node.end_byte].decode("utf-8", errors="replace") if node else ""


def _field(node, *names):
    for name in names:
        found = node.child_by_field_name(name)
        if found is not None:
            return found
    return None


def _walk(node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _declarator_name(node, data):
    """Follow declarators rather than accidentally choosing a parameter name."""
    while node is not None:
        if node.type in {"identifier", "field_identifier", "qualified_identifier", "operator_name", "destructor_name"}:
            return _text(node, data)
        child = _field(node, "declarator", "name")
        if child is None:
            child = next((c for c in node.named_children if "declarator" in c.type or c.type in {"identifier", "qualified_identifier"}), None)
        node = child
    return ""


def _function_spec(node, data, language):
    """Return a syntax definition and its owning assignment/decorator range."""
    if node.type not in FUNCTION_TYPES:
        return None
    body = _field(node, "body")
    if body is None:  # Declarations without implementations are not functions in this view.
        return None
    name_node = _field(node, "name")
    name = _text(name_node, data)
    owner = node
    if language in {"C", "C++"}:
        name = _declarator_name(_field(node, "declarator"), data)
    elif language == "R":
        parent = node.parent
        if parent and parent.type == "binary_operator":
            lhs, rhs = _field(parent, "lhs"), _field(parent, "rhs")
            other = rhs if lhs == node else lhs
            if other and other.type == "identifier":
                name, owner = _text(other, data), parent
        # An anonymous callback has no stable symbol, so do not invent a function name.
        if not name:
            return None
    elif node.type in {"arrow_function", "function_expression", "generator_function"}:
        parent = node.parent
        if parent and parent.type in {"variable_declarator", "pair", "assignment_expression", "public_field_definition", "field_definition"}:
            binding = _field(parent, "name", "key", "left", "property")
            if binding and binding.type in {"identifier", "property_identifier", "private_property_identifier"}:
                name, owner = _text(binding, data), parent
        if not name:
            return None
    if not name:
        return None
    if node.parent and node.parent.type == "decorated_definition":
        owner = node.parent
    return name, body, owner


def _raw_doc(owner, body, data, language):
    """Preserve comment spelling; do not present generated prose as source docs."""
    parts = []
    previous = owner.prev_named_sibling
    cursor = owner.start_byte
    while previous and "comment" in previous.type and not data[previous.end_byte:cursor].strip():
        gap = data[previous.end_byte:cursor]
        if gap.count(b"\n") > 2:
            break
        parts.insert(0, _text(previous, data))
        cursor, previous = previous.start_byte, previous.prev_named_sibling
    for child in body.named_children:
        if "comment" in child.type:
            parts.append(_text(child, data))
        elif language == "Python" and child.type == "expression_statement" and child.named_children and child.named_children[0].type in {"string", "concatenated_string"}:
            parts.append(_text(child, data))
            break
        else:
            break
    return "\n".join(parts)


def _parameter_names(node, data):
    """Extract binding positions, not every identifier in type annotations."""
    params = _field(node, "parameters", "parameter")
    if params is None:
        declarator = _field(node, "declarator")
        if declarator:
            params = next((n for n in _walk(declarator) if n.type in {"parameter_list", "parameters"}), None)
    names = set()
    if params:
        for p in params.named_children:
            binding = _field(p, "name", "pattern", "declarator")
            if p.type == "identifier":
                names.add(_text(p, data))
            elif binding:
                name = _declarator_name(binding, data)
                if name:
                    names.add(name)
    return names


def _python_scope_facts(source: str):
    """Build lexical bindings from Python AST without importing inspected code."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return {}, {"bound": set(), "imports": {}}
    result = {}

    def facts(body, arguments=None):
        bound, imports, declared = set(), {}, set()
        if arguments:
            bound.update(a.arg for a in arguments.posonlyargs + arguments.args + arguments.kwonlyargs)
            if arguments.vararg:
                bound.add(arguments.vararg.arg)
            if arguments.kwarg:
                bound.add(arguments.kwarg.arg)

        def visit(item, direct=False):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                if hasattr(item, "name"):
                    declared.add(item.name)
                return
            if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
                bound.add(item.id)
            if isinstance(item, (ast.Global, ast.Nonlocal)):
                bound.update(item.names)  # Conservatively leave explicit environment rebinding unresolved.
            if isinstance(item, ast.Import):
                for alias in item.names:
                    imports[alias.asname or alias.name.split(".")[0]] = (alias.name if alias.asname else alias.name.split(".")[0], None, 0, direct)
            if isinstance(item, ast.ImportFrom):
                for alias in item.names:
                    imports[alias.asname or alias.name] = (item.module or "", alias.name, item.level, direct)
            for child in ast.iter_child_nodes(item):
                visit(child, False)

        for item in body:
            visit(item, True)
        return {"bound": bound, "imports": imports, "declared": declared}

    for item in ast.walk(tree):
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result[(item.lineno, item.name)] = facts(item.body, item.args)
    return result, facts(tree.body)


def _local_bound_names(body, data, *, counts=False):
    """Conservative local reassignment detection for non-Python languages."""
    names = Counter()

    def visit(node):
        if node is not body and node.type in FUNCTION_TYPES | CLASS_TYPES:
            return
        binding = None
        if node.type in {"variable_declarator", "init_declarator", "let_declaration", "var_spec", "short_var_declaration"}:
            binding = _field(node, "name", "pattern", "declarator", "left")
        elif node.type in {"assignment", "assignment_expression"}:
            binding = _field(node, "left")
        elif node.type == "binary_operator" and any(c.type in {"<-", "<<-", "="} for c in node.children):
            binding = _field(node, "lhs")
        if binding:
            for child in _walk(binding):
                if child.type == "identifier":
                    names[_text(child, data)] += 1
        for child in node.named_children:
            visit(child)

    visit(body)
    return names if counts else set(names)


def _collect_file(path, relative, language, parser_name, data):
    """Extract definitions, containers and calls from one syntax tree."""
    parser = get_parser(parser_name)
    tree = parser.parse(data)
    records = []
    module_prefix = []
    if language in {"Go", "Java"}:
        pkg = next((n for n in tree.root_node.named_children if n.type in {"package_clause", "package_declaration"}), None)
        if pkg:
            p = _text(pkg, data).replace("package", "", 1).strip().rstrip(";")
            module_prefix = [("module", p)]

    def visit(node, contexts, parent_function=None):
        spec = _function_spec(node, data, language)
        if spec:
            name, body, owner = spec
            own_contexts = list(contexts)
            if language == "Go" and node.type == "method_declaration":
                receiver = _field(node, "receiver")
                if receiver and receiver.named_children:
                    receiver_type = _field(receiver.named_children[0], "type")
                    type_name = _text(receiver_type, data).lstrip("*")
                    if type_name:
                        own_contexts.append(("class", type_name))
            if "::" in name:
                pieces = name.split("::")
                own_contexts.extend(("module", part) for part in pieces[:-1])
                name = pieces[-1]
            container = ".".join(value for _, value in own_contexts)
            qualified = ".".join(filter(None, [container, name]))
            syntax_line = node.start_point.row + 1
            rec = {
                "id": "", "name": name, "qualified_name": qualified, "file": relative,
                "language": language, "line": owner.start_point.row + 1, "end_line": owner.end_point.row + 1,
                "signature": re.sub(r"\s+", " ", data[node.start_byte:body.start_byte].decode("utf-8", errors="replace")).strip(),
                "doc": _raw_doc(owner, body, data, language),
                "code_hash": hashlib.sha256(relative.encode() + b"\0" + data).hexdigest(),
                "code": _text(owner, data),
                "kind": "method" if any(k == "class" for k, _ in own_contexts) or node.type in {"method_definition", "method_declaration", "constructor_declaration"} else "function",
                "container": container,
                "_node": node, "_body": body, "_tree": tree, "_parser": parser, "_contexts": own_contexts,
                "_parent": parent_function, "_syntax_line": syntax_line,
                "_bound": _parameter_names(node, data) | _local_bound_names(body, data),
                "_calls": [], "_imports": {},
                "_declared": set(), "_anonymous": [],
                "_binding_owner": owner.type in {"binary_operator", "variable_declarator", "assignment_expression", "public_field_definition", "field_definition"},
            }
            records.append(rec)
            visit(body, own_contexts + [("function", name)], rec)
            return
        if node.type in {"lambda", "lambda_expression", "arrow_function", "function_expression", "generator_function"} or (language == "R" and node.type == "function_definition"):
            # Calls in anonymous callback bodies do not run merely because the
            # enclosing function constructs/passes the callback.
            if parent_function is not None:
                parent_function["_anonymous"].append(node.start_point.row + 1)
            return
        if node.type in CLASS_TYPES | MODULE_TYPES:
            named = _field(node, "name", "type")
            value = _text(named, data)
            if value:
                contexts = contexts + [("class" if node.type in CLASS_TYPES else "module", value)]
        if parent_function is not None and node.type in CALL_TYPES:
            function = _field(node, "function", "name")
            called = _text(function, data)
            if language == "Java":
                obj = _field(node, "object")
                if obj:
                    called = _text(obj, data) + "." + called
            if called:
                parent_function["_calls"].append({"name": called, "line": node.start_point.row + 1, "evidence": _text(node, data)[:800]})
        for child in node.named_children:
            visit(child, contexts, parent_function)

    visit(tree.root_node, module_prefix)
    # Named nested definitions are lexical declarations rather than callback variables.
    for rec in records:
        assignments = _local_bound_names(rec["_body"], data, counts=True)
        for child in records:
            if child["_parent"] is rec and child["_binding_owner"] and assignments[child["name"]] == 1:
                rec["_bound"].discard(child["name"])
    module_assignments = _local_bound_names(tree.root_node, data, counts=True)
    module_bound = set(module_assignments)
    for rec in records:
        if rec["_parent"] is None and rec["_binding_owner"] and module_assignments[rec["name"]] == 1:
            module_bound.discard(rec["name"])
    py_module = {"bound": module_bound, "imports": {}}
    if language == "Python":
        scopes, py_module = _python_scope_facts(data.decode("utf-8"))
        for rec in records:
            facts = scopes.get((rec["_syntax_line"], rec["name"]))
            if facts:
                rec["_bound"], rec["_imports"] = facts["bound"], facts["imports"]
                rec["_declared"] = facts["declared"]
    return records, tree.root_node.has_error, py_module


def _assign_ids(records):
    """Line numbers never identify symbols; duplicate definitions stay distinct."""
    groups = defaultdict(list)
    for record in records:
        groups[(record["file"], record["qualified_name"])].append(record)
    for (filename, qualified), group in groups.items():
        seen = Counter()
        for record in group:
            base = filename + "::" + qualified
            if len(group) > 1:
                digest = hashlib.sha256(re.sub(r"\s+", " ", record["code"]).encode()).hexdigest()[:12]
                seen[digest] += 1
                base += "#" + digest + ("-" + str(seen[digest]) if seen[digest] > 1 else "")
            record["id"] = base


def _python_import_targets(rec, expression, facts, by_file, root):
    """Resolve explicit source imports; never inspect sys.path or execute code."""
    head, *tail = expression.split(".")
    imports = dict(facts["imports"])
    ancestors = []
    parent = rec
    while parent:
        ancestors.append(parent)
        parent = parent["_parent"]
    for ancestor in reversed(ancestors):
        imports.update(ancestor["_imports"])
    if head not in imports:
        return [], None
    module, symbol, level, direct = imports[head]
    if symbol == "*":
        return [], None
    parts = module.split(".") if module else []
    parent_parts = list(Path(rec["file"]).parent.parts)
    if level:
        if level > len(parent_parts):
            return [], None
        parts = parent_parts[:len(parent_parts) - level + 1] + parts
    target_name = ".".join(([symbol] if symbol else []) + tail)
    if not target_name:
        return [], None
    paths = ["/".join(parts) + ".py", "/".join(parts + ["__init__.py"])]
    exact = [target for path in paths for target in by_file.get(path, []) if target["qualified_name"] == target_name]
    if exact:
        return exact, "resolved" if direct else "candidate"
    # Script-local imports depend on the launcher's sys.path, so remain candidates.
    if not level and parent_parts:
        sibling_paths = ["/".join(parent_parts + [p]) for p in paths]
        fallback = [target for path in sibling_paths for target in by_file.get(path, []) if target["qualified_name"] == target_name]
        if fallback:
            return fallback, "candidate"
    return [], None


def _resolve(rec, called, by_file, module_facts, root):
    """Return targets and confidence; unmatched names never gain global edges."""
    expression = re.sub(r"\s+", "", called)
    head = re.split(r"\.|::|->", expression)[0]
    # A callback parameter or locally assigned name can replace a same-named function.
    parent = rec
    while parent:
        if head in parent["_bound"] and head not in {"self", "cls", "this"}:
            return [], None, "参数或局部变量可决定调用目标，静态绑定未知"
        parent = parent["_parent"]
    local = by_file.get(rec["file"], [])
    if rec["language"] != "Python" and head in module_facts.get(rec["file"], {}).get("bound", set()):
        return [], None, "文件级名称存在变量绑定或重赋值，静态调用目标未知"
    if rec["language"] == "Python":
        facts = module_facts.get(rec["file"], {"bound": set(), "imports": {}})
        # A nested definition shadows a module import. Resolve this lexical
        # binding before consulting file-level imports with the same spelling.
        if "." not in expression:
            lexical = rec
            while lexical:
                if head in lexical["_declared"]:
                    if head in lexical["_imports"]:
                        return [], None, "局部导入与函数定义同名，未确定最终绑定"
                    targets = [target for target in local if target["qualified_name"] == lexical["qualified_name"] + "." + head]
                    if targets:
                        return targets, "resolved", "最近函数作用域中的嵌套定义绑定"
                lexical = lexical["_parent"]
        if head in facts["bound"]:
            return [], None, "模块名称存在赋值或重绑定，未确定运行时目标"
        if head in facts["imports"] and head in facts.get("declared", set()):
            return [], None, "导入与项目定义使用同名绑定，执行顺序可能改变目标"
        if head in rec["_imports"] and head in rec["_declared"]:
            return [], None, "局部导入与函数定义同名，未确定最终绑定"
        targets, status = _python_import_targets(rec, expression, facts, by_file, root)
        if targets:
            if any(target["qualified_name"].split(".")[0] in module_facts.get(target["file"], {}).get("bound", set()) for target in targets):
                return [], None, "导入文件中的目标名称存在重赋值，未确定最终导出值"
            return targets, status, "显式 Python 导入对应项目源码" if status == "resolved" else "导入依赖运行路径或条件"
        if head in facts["imports"] or head in rec["_imports"]:
            return [], None, "导入目标位于项目外、被重导出或无法定位"
    if re.fullmatch(r"[^\W\d]\w*", expression, flags=re.UNICODE):
        # Nested lexical definitions have priority, followed by visible containing scopes.
        scopes = [rec["qualified_name"]]
        contexts = rec["_contexts"]
        for size in range(len(contexts), -1, -1):
            selected = contexts[:size]
            if rec["language"] in {"Python", "JavaScript", "TypeScript"} and selected and selected[-1][0] == "class":
                continue
            scopes.append(".".join(value for _, value in selected))
        for scope in dict.fromkeys(scopes):
            qualified = ".".join(filter(None, [scope, expression]))
            targets = [target for target in local if target["qualified_name"] == qualified]
            if targets:
                return targets, "resolved", "同文件可见作用域中的静态名称绑定"
        # C/R/Go often span files without explicit per-function imports.  Do not
        # turn same-name matches across a directory into proven symbol binding.
        if rec["language"] in {"C", "C++", "R", "Go", "Rust"}:
            peers = [target for filename, values in by_file.items() if Path(filename).parent == Path(rec["file"]).parent for target in values if target["name"] == expression and target["language"] == rec["language"] and target["file"] != rec["file"] and target["kind"] == "function"]
            if peers:
                return peers, "candidate", "同目录同名定义候选；未解析包、链接或 source 执行环境"
        return [], None, "项目可见作用域内未定位；可能为内置、外部或动态函数"
    # Only a known receiver/container may produce member candidates. Arbitrary
    # obj.method calls must not attach to every project function named method.
    parts = re.split(r"\.|::|->", expression)
    if len(parts) >= 2 and all(re.fullmatch(r"[^\W\d]\w*", p, re.UNICODE) for p in parts):
        receiver, method = ".".join(parts[:-1]), parts[-1]
        if receiver in {"self", "cls", "this", "Self"}:
            class_index = next((i for i in range(len(rec["_contexts"]) - 1, -1, -1) if rec["_contexts"][i][0] == "class"), None)
            if class_index is not None:
                container = ".".join(v for _, v in rec["_contexts"][:class_index + 1])
                targets = [target for target in local if target["container"] == container and target["name"] == method]
                if targets:
                    return targets, "candidate", "当前类成员候选；继承、重绑定或动态分派未验证"
        targets = [target for target in local if target["qualified_name"] == expression or target["qualified_name"] == ".".join([rec["container"], expression])]
        if targets:
            return targets, "candidate", "限定名称对应本文件定义；接收者和重载仍需核实"
    return [], None, "动态属性、表达式或外部限定调用，未建立项目连线"


def analyze_project(root: Path) -> dict:
    """Analyze saved files only and return the stable, JSON-safe graph contract."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("项目路径不是可读取的目录")
    records, files, diagnostics = [], [], []
    module_facts = {}
    paths = []
    # Never follow directory/file symlinks, including links outside the project.
    for directory, dirs, filenames in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not (Path(directory) / d).is_symlink() and not (Path(directory) / d).is_junction())
        for filename in sorted(filenames):
            path = Path(directory) / filename
            if path.suffix.lower() in EXTENSIONS and not path.is_symlink() and path.resolve().is_relative_to(root):
                paths.append(path)
                if len(paths) > MAX_FILES:
                    break
        if len(paths) > MAX_FILES:
            diagnostics.append({"file": "", "level": "warning", "message": f"源文件超过 {MAX_FILES} 个；仅分析前 {MAX_FILES} 个"})
            break
    for path in paths[:MAX_FILES]:
        relative = path.relative_to(root).as_posix()
        language, parser_name = EXTENSIONS[path.suffix.lower()]
        item = {"path": relative, "language": language, "error": None, "functions": 0}
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("文件超过 1 MiB 限制，已跳过")
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError("文件超过 1 MiB 限制，已跳过")
            # UTF-8 BOM is accepted; silently guessing legacy encodings would corrupt locations.
            source = raw.decode("utf-8-sig")
            data = source.encode("utf-8")
            extracted, has_error, facts = _collect_file(path, relative, language, parser_name, data)
            for record in extracted:
                record["_syntax_error"] = has_error
            records.extend(extracted)
            module_facts[relative] = facts
            item["functions"] = len(extracted)
            if has_error:
                item["error"] = "存在语法错误；当前仅显示可解析的部分，调用关系可能不完整"
                diagnostics.append({"file": relative, "level": "warning", "message": item["error"]})
        except Exception as exc:
            item["error"] = f"解析失败：{type(exc).__name__}: {exc}"
            diagnostics.append({"file": relative, "level": "error", "message": item["error"]})
        files.append(item)
    _assign_ids(records)
    by_file = defaultdict(list)
    for record in records:
        by_file[record["file"]].append(record)
    edges, unresolved = [], []
    edge_seen = set()
    for record in records:
        for line in record["_anonymous"]:
            unresolved.append({"source": record["id"], "name": "<anonymous callback>", "line": line, "file": record["file"], "reason": "匿名回调体暂未建立独立节点，其内部调用未归入外层函数"})
        for call in record["_calls"]:
            targets, status, reason = _resolve(record, call["name"], by_file, module_facts, root)
            if not targets:
                unresolved.append({"source": record["id"], "name": call["name"], "line": call["line"], "file": record["file"], "reason": reason})
                continue
            if len(targets) > 1:
                status = "candidate"
                reason = "存在多个同名、重载或重复定义候选；" + reason
            if record["_syntax_error"] or any(target["_syntax_error"] for target in targets):
                status = "candidate"
                reason = "相关文件存在语法错误；" + reason
            for target in targets:
                key = (record["id"], target["id"], call["line"], call["name"])
                if key in edge_seen:
                    continue
                edge_seen.add(key)
                edges.append({"id": hashlib.sha256(repr(key).encode()).hexdigest()[:20], "source": record["id"], "target": target["id"], "kind": "call", "status": status, "label": call["name"], "line": call["line"], "evidence": reason + "\n" + call["evidence"]})
    nodes = [{key: value for key, value in record.items() if not key.startswith("_")} for record in records]
    return {"nodes": nodes, "edges": edges, "unresolved": unresolved, "files": files, "diagnostics": diagnostics, "stats": {"files": len(files), "functions": len(nodes), "calls": len(edges), "unresolved": len(unresolved)}}
