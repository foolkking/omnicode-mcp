import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

class GitResult:
    """
    Structured result of a Git command execution.
    """
    def __init__(self, success: bool, output: str = "", error: str = "", return_code: int = 0, data: dict = None):
        self.success = success
        self.output = output
        self.error = error
        self.return_code = return_code
        self.data = data or {}

class GitManager:
    """
    Provides Git operation logic for compatibility with the legacy router endpoints.
    """
    def __init__(self, working_dir: str):
        self.working_dir = os.path.abspath(working_dir)
        self.git_dir = Path(self.working_dir) / ".git"
        self.is_git_repo = os.path.exists(self.git_dir)

    def get_stats(self) -> dict:
        return {
            "is_git_repo": self.is_git_repo,
            "working_directory": self.working_dir
        }

    async def initialize_codebase_repo(self) -> GitResult:
        try:
            if not self.is_git_repo:
                res = subprocess.run(["git", "init"], cwd=self.working_dir, capture_output=True, text=True)
                if res.returncode == 0:
                    self.is_git_repo = True
                    return GitResult(True, output=res.stdout)
                else:
                    return GitResult(False, error=res.stderr, return_code=res.returncode)
            return GitResult(True, output="Already a git repository")
        except Exception as e:
            return GitResult(False, error=str(e))

    async def _run_git(self, args: List[str]) -> GitResult:
        try:
            res = subprocess.run(["git"] + args, cwd=self.working_dir, capture_output=True, text=True, encoding="utf-8", errors="ignore")
            success = (res.returncode == 0)
            return GitResult(
                success=success,
                output=res.stdout,
                error=res.stderr if not success else "",
                return_code=res.returncode
            )
        except Exception as e:
            return GitResult(False, error=str(e))

    async def get_status(self) -> GitResult:
        res = await self._run_git(["status", "--porcelain"])
        if not res.success:
            return res

        # Get current branch name
        branch_res = await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        current_branch = branch_res.output.strip() if branch_res.success else "master"

        modified = []
        untracked = []
        staged = []

        lines = res.output.splitlines()
        for line in lines:
            if len(line) < 3:
                continue
            status = line[:2]
            file = line[3:].strip()
            if status[0] in ['M', 'A', 'D', 'R']:
                staged.append(file)
            elif status[1] in ['M', 'D']:
                modified.append(file)
            elif status == '??':
                untracked.append(file)

        res.data = {
            "status": {
                "current_branch": current_branch,
                "modified_files": modified,
                "untracked_files": untracked,
                "staged_files": staged
            }
        }
        return res

    async def get_branches(self) -> GitResult:
        res = await self._run_git(["branch", "--list"])
        if not res.success:
            return res

        branches = []
        lines = res.output.splitlines()
        for line in lines:
            if not line:
                continue
            is_current = line.startswith("*")
            name = line.replace("*", "").strip()
            branches.append({
                "name": name,
                "is_current": is_current
            })

        res.data = {"branches": branches}
        return res

    async def get_log(self, max_commits: int = 10, file_path: Optional[str] = None) -> GitResult:
        args = ["log", f"-n{max_commits}", "--oneline"]
        if file_path:
            args.append(file_path)
        res = await self._run_git(args)
        if not res.success:
            return res

        commits = []
        lines = res.output.splitlines()
        for line in lines:
            if not line:
                continue
            parts = line.split(" ", 1)
            commit_hash = parts[0]
            msg = parts[1] if len(parts) > 1 else ""
            commits.append({
                "hash": commit_hash,
                "message": msg
            })

        res.data = {"commits": commits}
        return res

    async def get_diff(self, file_path: Optional[str] = None, cached: bool = False) -> GitResult:
        args = ["diff"]
        if cached:
            args.append("--cached")
        if file_path:
            args.append(file_path)
        res = await self._run_git(args)
        if res.success:
            res.data = {"diff": res.output}
        return res

    async def add_files(self, files: List[str]) -> GitResult:
        return await self._run_git(["add"] + files)

    async def commit(self, message: str, files: Optional[List[str]] = None) -> GitResult:
        if files:
            add_res = await self.add_files(files)
            if not add_res.success:
                return add_res
        res = await self._run_git(["commit", "-m", message])
        if res.success:
            hash_res = await self._run_git(["rev-parse", "HEAD"])
            if hash_res.success:
                res.data = {"commit_hash": hash_res.output.strip()}
        return res

    async def get_file_blame(self, file_path: str) -> GitResult:
        res = await self._run_git(["blame", file_path])
        if res.success:
            res.data = {"blame": res.output}
        return res

    async def create_branch(self, branch_name: str, switch_to: bool = True) -> GitResult:
        if switch_to:
            return await self._run_git(["checkout", "-b", branch_name])
        else:
            return await self._run_git(["branch", branch_name])

    async def get_current_branch(self) -> GitResult:
        res = await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        if res.success:
            res.data = {"current_branch": res.output.strip()}
        return res

    async def checkout_branch(self, branch_name: str) -> GitResult:
        return await self._run_git(["checkout", branch_name])

    async def merge_branch(self, branch_name: str, message: str) -> GitResult:
        return await self._run_git(["merge", branch_name, "-m", message])

    async def list_session_branches(self) -> GitResult:
        res = await self.get_branches()
        if not res.success:
            return res

        # Sessions = any branch other than the conventional "trunk" names.
        # Previously this filtered to only ``ai-session-*`` / ``session-*`` —
        # user-named branches (like "你好" or feature branches) were silently
        # excluded from the session list UI.
        TRUNK = {"master", "main", "trunk", "develop"}
        session_branches = []
        branches = res.data.get("branches", [])
        for branch in branches:
            name = branch.get("name", "")
            if not name or name in TRUNK:
                continue
            session_branches.append(branch)

        res.data = {"sessions": session_branches}
        return res

    async def delete_branch(self, branch_name: str, force: bool = True) -> GitResult:
        flag = "-D" if force else "-d"
        return await self._run_git(["branch", flag, branch_name])
