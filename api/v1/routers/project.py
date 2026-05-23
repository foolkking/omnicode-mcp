"""
Project and directory operation endpoints
Handles project context, directory listing, and tree views
"""

from fastapi import APIRouter, Query

from core import get_project_manager
from utils import create_error_response, create_success_response

router = APIRouter(prefix="/project", tags=["project"])


@router.get("/context")
async def get_project_context(
    operation: str = Query(
        "info", description="Context operation: info, structure, dependencies"
    ),
    max_depth: int = Query(5, description="Maximum depth for structure"),
    include_hidden: bool = Query(False, description="Include hidden files"),
):
    """Get enhanced project context information"""
    try:
        project_manager = get_project_manager()
        if not project_manager:
            return create_error_response("Project manager not initialized", 500)

        if operation == "info":
            info = project_manager.get_project_info()

            result = {
                "operation": "info",
                "working_directory": info["working_directory"],
                "summary": {
                    "total_files": info["total_files"],
                    "total_size": project_manager.format_size(info["total_size"]),
                    "total_lines": f"{info['total_lines']:,}",
                },
                "project_files": info["project_files"],
                "file_types": info["top_file_types"],
                "detailed_info": info,
            }

            return create_success_response(result)

        elif operation == "structure":
            structure = project_manager.get_project_structure(max_depth, include_hidden)
            info = project_manager.get_project_info()

            result = {
                "operation": "structure",
                "max_depth": max_depth,
                "include_hidden": include_hidden,
                "summary": {
                    "total_files": info["total_files"],
                    "total_size": project_manager.format_size(info["total_size"]),
                    "total_lines": f"{info['total_lines']:,}",
                },
                "tree_structure": structure,
            }

            return create_success_response(result)

        elif operation == "dependencies":
            deps = project_manager.get_dependencies_info()

            result = {
                "operation": "dependencies",
                "dependency_files": list(deps.keys()),
                "dependencies": deps,
            }

            return create_success_response(result)

        else:
            return create_error_response(
                f"Unknown operation: {operation}. Valid: info, structure, dependencies",
                400,
            )

    except Exception as e:
        return create_error_response(f"Failed to get project context: {str(e)}", 500)


@router.get("/graph")
async def get_code_graph(
    path: str = Query(..., description="File path or directory path to analyze"),
    max_depth: int = Query(3, description="Maximum directory depth to walk if path is a directory"),
):
    """
    Analyze and extract a code graph showing symbol definitions, imports, and relationships.
    """
    try:
        import os
        from pathlib import Path

        from core import get_ast_parser, get_settings

        settings = get_settings()
        ast_parser = get_ast_parser()

        if not ast_parser:
            return create_error_response("AST Parser not initialized", 500)

        full_path = Path(settings.WORKING_DIR) / path
        if not full_path.exists():
            return create_error_response(f"Path does not exist: {path}", 404)

        # Determine if it's file or directory
        if full_path.is_file():
            # Code graph for a single file
            rel_path = str(full_path.relative_to(settings.WORKING_DIR))
            language = full_path.suffix.lstrip(".") or "python"

            try:
                with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except Exception as e:
                return create_error_response(f"Failed to read file: {e}", 500)

            tree = ast_parser.parse(content, language)

            # Extract symbols and imports using the parser
            symbols = []
            imports = []

            if tree:
                root = tree.root_node

                # Helper to walk nodes recursively
                def extract_node_symbols(node, parent_class=None):
                    node_type = node.type

                    if node_type in ['function_definition', 'class_definition', 'function_declaration', 'method_definition']:
                        # Try to get the name of the function or class
                        name = "unknown"
                        for child in node.children:
                            if child.type == 'identifier':
                                try:
                                    name = content.encode('utf-8')[child.start_byte:child.end_byte].decode('utf-8')
                                except:
                                    pass
                                break

                        start_line = node.start_point[0] + 1
                        end_line = node.end_point[0] + 1

                        symbols.append({
                            "name": name,
                            "type": node_type.replace('_definition', '').replace('_declaration', ''),
                            "line_start": start_line,
                            "line_end": end_line,
                            "parent": parent_class
                        })

                        # Recurse with class context
                        new_parent = name if node_type == 'class_definition' else parent_class
                        for child in node.children:
                            extract_node_symbols(child, new_parent)
                    else:
                        # Check for imports
                        if node_type in ['import_statement', 'import_from_statement', 'lexical_declaration']:
                            try:
                                import_text = content.encode('utf-8')[node.start_byte:node.end_byte].decode('utf-8')
                                imports.append(import_text.strip())
                            except:
                                pass

                        for child in node.children:
                            extract_node_symbols(child, parent_class)

                extract_node_symbols(root)

            # Build ASCII representation of the file relationships
            ascii_graph = f"📄 Code Graph for {rel_path} ({language})\n"
            ascii_graph += "=" * 50 + "\n"

            if imports:
                ascii_graph += "📥 Imports:\n"
                for imp in imports[:10]:
                    ascii_graph += f"  ├── {imp}\n"
                if len(imports) > 10:
                    ascii_graph += f"  └── ... and {len(imports) - 10} more imports\n"
                ascii_graph += "\n"

            if symbols:
                ascii_graph += "🏛️ Symbol Hierarchy & Structures:\n"
                classes = [s for s in symbols if s["type"] == "class"]
                standalone_funcs = [s for s in symbols if s["type"] in ["function", "method"] and not s["parent"]]
                methods = [s for s in symbols if s["type"] in ["function", "method"] and s["parent"]]

                for cls in classes:
                    ascii_graph += f"  ├── 📦 Class: {cls['name']} (Lines {cls['line_start']}-{cls['line_end']})\n"
                    cls_methods = [m for m in methods if m["parent"] == cls["name"]]
                    for i, meth in enumerate(cls_methods):
                        connector = "  │   └── " if i == len(cls_methods)-1 else "  │   ├── "
                        ascii_graph += f"{connector}⚙️ {meth['name']}()\n"

                for func in standalone_funcs:
                    ascii_graph += f"  ├── ⚙️ Function: {func['name']}() (Lines {func['line_start']}-{func['line_end']})\n"
            else:
                ascii_graph += "No major symbol definitions found in file.\n"

            result = {
                "path": path,
                "is_file": True,
                "language": language,
                "symbols_count": len(symbols),
                "imports_count": len(imports),
                "graph_text": ascii_graph,
                "symbols": symbols,
                "imports": imports
            }
            return create_success_response(result)

        else:
            # Code graph for a directory
            rel_path = str(full_path.relative_to(settings.WORKING_DIR))
            ascii_graph = f"📁 Directory Code Graph for: {rel_path}\n"
            ascii_graph += "=" * 50 + "\n"

            files_analyzed = []
            file_symbols = {}
            file_imports = {}

            valid_extensions = {".py", ".js", ".ts", ".jsx", ".tsx", ".cpp", ".h"}

            for root, dirs, files in os.walk(full_path):
                # Filter directories
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "__pycache__", ".venv"}]

                # Check depth
                depth = len(Path(root).relative_to(full_path).parts)
                if depth >= max_depth:
                    continue

                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in valid_extensions:
                        file_abs = Path(root) / file
                        file_rel = str(file_abs.relative_to(settings.WORKING_DIR))

                        try:
                            with open(file_abs, "r", encoding="utf-8", errors="ignore") as f:
                                content = f.read()
                        except:
                            continue

                        # Parse
                        language = ext.lstrip(".")
                        if language == "py":
                            language = "python"
                        elif language in ["js", "jsx"]:
                            language = "javascript"
                        elif language in ["ts", "tsx"]:
                            language = "typescript"

                        tree = ast_parser.parse(content, language)

                        symbols = []
                        imports = []
                        if tree:
                            def extract_node_symbols(node):
                                if node.type in ['function_definition', 'class_definition', 'function_declaration', 'method_definition']:
                                    name = "unknown"
                                    for child in node.children:
                                        if child.type == 'identifier':
                                            try:
                                                name = content.encode('utf-8')[child.start_byte:child.end_byte].decode('utf-8')
                                            except:
                                                pass
                                            break
                                    symbols.append({"name": name, "type": node.type})
                                elif node.type in ['import_statement', 'import_from_statement']:
                                    try:
                                        import_text = content.encode('utf-8')[node.start_byte:node.end_byte].decode('utf-8')
                                        imports.append(import_text.strip())
                                    except:
                                        pass
                                for child in node.children:
                                    extract_node_symbols(child)
                            extract_node_symbols(tree.root_node)

                        files_analyzed.append(file_rel)
                        file_symbols[file_rel] = symbols
                        file_imports[file_rel] = imports

            # Build ASCII Graph
            ascii_graph += f"📊 Analyzed {len(files_analyzed)} files across depth {max_depth}\n\n"
            for f_rel in files_analyzed[:15]:
                ascii_graph += f"📄 {os.path.basename(f_rel)}\n"

                # Display imports
                imps = file_imports.get(f_rel, [])
                if imps:
                    ascii_graph += "  ├── 📥 Imports:\n"
                    for imp in imps[:3]:
                        ascii_graph += f"  │   ├── {imp}\n"
                    if len(imps) > 3:
                        ascii_graph += f"  │   └── ... and {len(imps)-3} more\n"

                # Display symbols
                syms = file_symbols.get(f_rel, [])
                if syms:
                    ascii_graph += "  └── 🏛️ Major Symbols:\n"
                    for i, sym in enumerate(syms[:5]):
                        connector = "      └── " if i == len(syms[:5])-1 else "      ├── "
                        sym_name = sym["name"]
                        sym_type = sym["type"].replace('_definition', '').replace('_declaration', '')
                        ascii_graph += f"{connector}{sym_type}: {sym_name}\n"
                    if len(syms) > 5:
                        ascii_graph += f"      └── ... and {len(syms)-5} more symbols\n"
                ascii_graph += "\n"

            if len(files_analyzed) > 15:
                ascii_graph += f"... and {len(files_analyzed)-15} more files inside {rel_path} directory\n"

            result = {
                "path": path,
                "is_file": False,
                "files_count": len(files_analyzed),
                "graph_text": ascii_graph,
                "files": files_analyzed
            }
            return create_success_response(result)

    except Exception as e:
        return create_error_response(f"Failed to generate code graph: {str(e)}", 500)

