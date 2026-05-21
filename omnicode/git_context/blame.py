from typing import List, Optional
from pydantic import BaseModel
import git
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

class BlameLine(BaseModel):
    line_number: int
    commit_hash: str
    author: str
    date: str
    content: str
    commit_message: str

class ChangeContext(BaseModel):
    commit_hash: str
    author: str
    date: str
    commit_message: str
    related_issues: List[str]

class GitBlameAnalyzer:
    """
    Analyzes git blame and history to provide time-dimension context.
    """
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        try:
            self.repo = git.Repo(self.repo_path)
        except git.exc.InvalidGitRepositoryError:
            logger.warning(f"No git repository found at {repo_path}")
            self.repo = None

    def get_blame(self, file_path: str, start_line: int, end_line: int) -> List[BlameLine]:
        """Get blame info for a specific range of lines"""
        if not self.repo:
            return []
            
        full_path = self.repo_path / file_path
        if not full_path.exists():
            return []
            
        try:
            # Note: gitpython blame returns tuples of (commit, lines)
            # We need to map this to line numbers
            blame_data = []
            
            # Using raw git command for simpler line-by-line output
            # Format: commit author date content
            blame_out = self.repo.git.blame("-L", f"{start_line},{end_line}", "--line-porcelain", file_path)
            
            current_commit = None
            current_author = None
            current_date = None
            current_msg = None
            line_idx = start_line
            
            lines = blame_out.splitlines()
            for line in lines:
                if len(line) == 40 and " " not in line: # Commit hash
                    # Skip for now, handled by line-porcelain parsing logic
                    pass 
            
            # Basic fallback for MVP - full parsing of --line-porcelain is complex
            # We'll just use the standard blame for now and extract the hash
            std_blame = self.repo.git.blame("-L", f"{start_line},{end_line}", file_path)
            for i, line in enumerate(std_blame.splitlines()):
                parts = line.split(" ", 1)
                commit_hash = parts[0]
                
                # Fetch commit info
                try:
                    commit = self.repo.commit(commit_hash)
                    blame_data.append(BlameLine(
                        line_number=start_line + i,
                        commit_hash=commit_hash,
                        author=commit.author.name,
                        date=str(commit.authored_datetime),
                        content=line,
                        commit_message=commit.message.strip()
                    ))
                except Exception:
                    continue
                    
            return blame_data
        except Exception as e:
            logger.error(f"Failed to get blame for {file_path}: {e}")
            return []

    def get_change_context(self, file_path: str, line_range: tuple[int, int]) -> Optional[ChangeContext]:
        """Summarize the context of changes in a line range"""
        blame_lines = self.get_blame(file_path, line_range[0], line_range[1])
        if not blame_lines:
            return None
            
        # Find the most frequent or most recent commit in this range
        commits = {}
        for line in blame_lines:
            if line.commit_hash not in commits:
                commits[line.commit_hash] = {
                    "count": 0,
                    "author": line.author,
                    "date": line.date,
                    "message": line.commit_message
                }
            commits[line.commit_hash]["count"] += 1
            
        if not commits:
            return None
            
        # Select the commit that affected the most lines in this range
        primary_commit_hash = max(commits.keys(), key=lambda k: commits[k]["count"])
        c_info = commits[primary_commit_hash]
        
        import re
        # Simple issue extractor (e.g. #123 or ISS-123)
        issues = re.findall(r'(?:#|ISSUE-|[A-Z]+-)\d+', c_info["message"])
        
        return ChangeContext(
            commit_hash=primary_commit_hash,
            author=c_info["author"],
            date=c_info["date"],
            commit_message=c_info["message"],
            related_issues=issues
        )
