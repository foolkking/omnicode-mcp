import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    """
    Application Settings using pydantic-settings.
    Automatically loads from environment variables and .env file.
    """
    # API Information
    API_TITLE: str = Field(default="OmniCode-MCP")
    API_DESCRIPTION: str = Field(default="Next-gen Codebase MCP Server")
    API_VERSION: str = Field(default="1.0.0-beta")

    # Working Directory
    WORKING_DIR: str = Field(default=os.getcwd())

    # FastAPI Server
    API_HOST: str = Field(default="127.0.0.1")
    API_PORT: int = Field(default=6789)

    # CORS Configuration
    CORS_ORIGINS: List[str] = Field(default=["*"]) # Should be restricted in production
    CORS_CREDENTIALS: bool = Field(default=True)
    CORS_METHODS: List[str] = Field(default=["*"])
    CORS_HEADERS: List[str] = Field(default=["*"])

    # LLM Provider Configuration
    DEFAULT_LLM_PROVIDER: str = Field(default="gemini")
    DEFAULT_LLM_MODEL: str = Field(default="gemini-2.5-flash")

    # API Keys (Loaded from .env)
    GEMINI_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    DEEPSEEK_API_KEY: Optional[str] = None

    # Provider registry (custom external LLM API integrations)
    PROVIDER_DB_PATH: str = Field(default=".data/providers.db")

    # File-system browser (native OS file picker backend)
    FS_BROWSER_ENABLED: bool = Field(default=True)
    FS_BROWSER_MAX_FILE_BYTES: int = Field(default=2 * 1024 * 1024)  # 2 MiB
    FS_BROWSER_DENY_PATTERNS: List[str] = Field(default=[
        # Linux/macOS sensitive paths
        "/etc/shadow", "/etc/sudoers", "/etc/ssh", "/root/.ssh",
        "/proc", "/sys", "/dev",
        # Windows protected directories
        "C:\\Windows\\System32\\config",
        "C:\\Windows\\System32\\drivers",
        "C:\\Windows\\System32\\LogFiles",
    ])

    # Search Configuration
    EMBEDDING_MODEL: str = Field(default="sentence-transformers/all-MiniLM-L6-v2")
    MAX_SEARCH_RESULTS: int = Field(default=10)
    FAISS_INDEX_TYPE: str = Field(default="Flat")

    # Code Quality Configuration
    QUALITY_THRESHOLD: float = Field(default=0.8)
    AUTO_FORMAT_PYTHON: bool = Field(default=True)
    AUTO_FORMAT_JS: bool = Field(default=True)

    # Git Configuration
    CODEBASE_GIT_DIR: str = Field(default=".codebase")
    DEFAULT_BRANCH: str = Field(default="main")
    AUTO_COMMIT_ENABLED: bool = Field(default=True)

    # Memory System Configuration
    MEMORY_CONTEXT_MAX: int = Field(default=10)
    MEMORY_RECENT_DAYS: int = Field(default=30)
    MEMORY_MIN_IMPORTANCE: int = Field(default=3)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def update_working_directory(self, new_dir: str) -> None:
        """Update working directory with validation"""
        path = Path(new_dir)
        if not path.exists():
            logger.error(f"Directory {new_dir} does not exist.")
            raise FileNotFoundError(f"Directory {new_dir} does not exist.")
        if not path.is_dir():
            logger.error(f"Path {new_dir} is not a directory.")
            raise NotADirectoryError(f"Path {new_dir} is not a directory.")

        self.WORKING_DIR = str(path.absolute())
        logger.info(f"Working directory updated to: {self.WORKING_DIR}")

@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance"""
    return Settings()
