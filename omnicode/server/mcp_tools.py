from mcp.server.fastmcp import FastMCP
from typing import Optional
import logging

from ..git_context.blame import GitBlameAnalyzer
from ..guard.analyzer import ProactiveGuard

logger = logging.getLogger(__name__)

# This would typically wrap the existing mcp_server.py logic,
# but we are just defining the new tools here for the OmniCode integration.

def register_omnicode_tools(mcp: FastMCP, workspace_dir: str):
    
    blame_analyzer = GitBlameAnalyzer(workspace_dir)
    guard = ProactiveGuard()

    @mcp.tool()
    async def blame_tool(file_path: str, start_line: int, end_line: int) -> str:
        """
        Get git blame information for a range of lines to understand the history 
        and context of why code was written a certain way.
        
        Args:
            file_path: Path to the file
            start_line: Starting line number (1-indexed)
            end_line: Ending line number (1-indexed)
        """
        try:
            blame_data = blame_analyzer.get_blame(file_path, start_line, end_line)
            if not blame_data:
                return f"No blame data found for {file_path}:{start_line}-{end_line}"
                
            result = []
            for line in blame_data:
                result.append(
                    f"Line {line.line_number} | {line.commit_hash[:8]} | {line.author} | {line.date[:10]} | "
                    f"Msg: {line.commit_message[:50]}... | {line.content}"
                )
            return "\n".join(result)
        except Exception as e:
            return f"Error getting blame: {str(e)}"

    @mcp.tool()
    async def guard_tool(file_path: str) -> str:
        """
        Run static analysis checks (mypy, ruff, eslint, etc.) on a file 
        to ensure code quality and prevent errors.
        
        Args:
            file_path: Path to the file to check
        """
        try:
            result = await guard.check(file_path)
            if result.is_clean:
                return f"✅ {file_path} passed all static analysis checks."
            else:
                return (
                    f"❌ {file_path} failed checks.\n\n"
                    f"Errors:\n{result.errors}\n\n"
                    f"Warnings:\n{result.warnings}"
                )
        except Exception as e:
            return f"Error running guard checks: {str(e)}"

    @mcp.tool()
    async def change_context_tool(file_path: str, start_line: int, end_line: int) -> str:
        """
        Get a summarized context of changes for a specific line range, 
        including related issues and primary commit messages.
        
        Args:
            file_path: Path to the file
            start_line: Starting line number (1-indexed)
            end_line: Ending line number (1-indexed)
        """
        try:
            context = blame_analyzer.get_change_context(file_path, (start_line, end_line))
            if not context:
                return f"No context found for {file_path}:{start_line}-{end_line}"
                
            return (
                f"Change Context for {file_path}:{start_line}-{end_line}\n"
                f"Primary Commit: {context.commit_hash}\n"
                f"Author: {context.author}\n"
                f"Date: {context.date}\n"
                f"Message: {context.commit_message}\n"
                f"Related Issues: {', '.join(context.related_issues) if context.related_issues else 'None found'}"
            )
        except Exception as e:
            return f"Error getting change context: {str(e)}"

    logger.info("Registered OmniCode specialized tools.")
