import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

class DirectoryLister:
    """
    Traverses directories and generates rich metadata summaries
    and ASCII-formatted tree views of the workspace files.
    """
    def __init__(self, working_dir: str):
        self.working_dir = os.path.abspath(working_dir)

    def format_size(self, size_in_bytes: int) -> str:
        if size_in_bytes < 1024:
            return f"{size_in_bytes}B"
        elif size_in_bytes < 1024 * 1024:
            return f"{size_in_bytes / 1024:.1f}KB"
        else:
            return f"{size_in_bytes / (1024 * 1024):.1f}MB"

    def list_directory(
        self,
        directory_path: str = ".",
        max_depth: int = 2,
        include_hidden: bool = False,
        show_metadata: bool = True,
        respect_gitignore: bool = True,
        files_only: bool = False,
        dirs_only: bool = False,
    ) -> Dict[str, Any]:

        # Determine actual absolute path
        if directory_path == "." or not directory_path:
            abs_dir = self.working_dir
        else:
            abs_dir = os.path.abspath(os.path.join(self.working_dir, directory_path))

        if not os.path.exists(abs_dir):
            return {"error": f"Directory does not exist: {directory_path}"}
        if not os.path.isdir(abs_dir):
            return {"error": f"Path is not a directory: {directory_path}"}

        items = []
        summary = {"total_files": 0, "total_directories": 0, "total_size_bytes": 0}

        def traverse(current_dir: str, current_depth: int, prefix_str: str):
            if current_depth > max_depth:
                return

            try:
                entries = sorted(os.scandir(current_dir), key=lambda e: (not e.is_dir(), e.name.lower()))
            except Exception as e:
                logger.warning(f"Error scanning directory {current_dir}: {e}")
                return

            # Exclude hidden directories/files and build/cache folders by default
            skip_patterns = {".git", "__pycache__", "node_modules", ".venv", ".data"}

            filtered_entries = []
            for entry in entries:
                if not include_hidden and entry.name.startswith("."):
                    continue
                if respect_gitignore and entry.name in skip_patterns:
                    continue
                filtered_entries.append(entry)

            for i, entry in enumerate(filtered_entries):
                is_last = (i == len(filtered_entries) - 1)
                connector = "└── " if is_last else "├── "
                item_prefix = prefix_str + connector
                next_prefix = prefix_str + ("    " if is_last else "│   ")

                rel_path = os.path.relpath(entry.path, self.working_dir)

                is_dir = entry.is_dir()
                size = 0
                lines = 0

                if is_dir:
                    if files_only:
                        pass
                    else:
                        summary["total_directories"] += 1
                        # Get a quick count of items inside
                        try:
                            file_count = len(os.listdir(entry.path))
                        except Exception:
                            file_count = 0

                        items.append({
                            "name": entry.name,
                            "path": rel_path,
                            "is_directory": True,
                            "tree_prefix": item_prefix,
                            "file_count": file_count
                        })

                    traverse(entry.path, current_depth + 1, next_prefix)
                else:
                    if dirs_only:
                        continue

                    summary["total_files"] += 1
                    try:
                        stat = entry.stat()
                        size = stat.st_size
                        summary["total_size_bytes"] += size
                    except Exception:
                        pass

                    # Quick line count for text files
                    if size < 1024 * 1024:  # Only count lines for files < 1MB
                        try:
                            with open(entry.path, "r", encoding="utf-8", errors="ignore") as f:
                                lines = sum(1 for _ in f)
                        except Exception:
                            pass

                    items.append({
                        "name": entry.name,
                        "path": rel_path,
                        "is_directory": False,
                        "tree_prefix": item_prefix,
                        "size": size,
                        "line_count": lines
                    })

        traverse(abs_dir, 1, "")

        return {
            "directory": os.path.relpath(abs_dir, self.working_dir),
            "items": items,
            "summary": f"{summary['total_files']} files, {summary['total_directories']} folders, Total size: {self.format_size(summary['total_size_bytes'])}"
        }
