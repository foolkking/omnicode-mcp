"""
Application lifespan management
Handles initialization and shutdown of services
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import get_settings
from core.dependencies import (
    set_ast_parser,
    set_directory_lister,
    set_edit_pipeline,
    set_git_manager,
    set_llm_router,
    set_memory_manager,
    set_project_manager,
    set_search_engine,
    set_write_pipeline,
)
from memory_system import MemoryManager
from omnicode.ast_engine.parser import UnifiedASTParser
from omnicode.git_context import GitManager
from omnicode.llm.router import LLMRouter
from omnicode.pipelines.edit import EditPipeline
from omnicode.pipelines.write import WritePipeline
from omnicode.search import DirectoryLister, SemanticSearchEngine
from project_structure.project_manager import ProjectStructureManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Force HuggingFace / sentence-transformers into offline mode so the model
# is loaded from the local cache without any network round-trips.
# This prevents the ~2-minute startup delay when huggingface.co is unreachable.
# The env vars are set here (in addition to .env) so they take effect even
# when the process is launched without the .env file being loaded first.
# ---------------------------------------------------------------------------
for _hf_var in ("TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE", "HF_HUB_OFFLINE"):
    os.environ.setdefault(_hf_var, "1")


async def initialize_services() -> None:
    """Initialize all application services"""
    settings = get_settings()
    working_dir = settings.WORKING_DIR

    logger.info(f"FastAPI Server starting - Working directory: {working_dir}")

    # ------------------------------------------------------------------
    # STAGE 9.9 — install the streaming log handler on the root logger so
    # every subsequent ``logger.info(...)`` call gets fanned out to any
    # WebSocket subscriber on `/logs/stream`.
    # ------------------------------------------------------------------
    try:
        from core import log_stream

        log_stream.install()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not install log stream handler: %s", exc)

    try:
        # Honour the feature flags from omnicode_core/config/features.py.
        # When `llm_router` is off the entire LLM stack (provider
        # registry, router, write/edit pipelines that depend on it) is
        # skipped — core search / read / patch still works without an
        # LLM API key.
        from omnicode_core.config.features import get_features
        features = get_features()
        if not features.llm_router:
            logger.info(
                "⚡ LLM router disabled by feature flag "
                "(OMNICODE_LLM_ROUTER=false). Skipping provider registry, "
                "router, write pipeline and edit pipeline initialization."
            )
        else:
            # Initialize LLM Router (Model Gateway)
            from omnicode.config.settings import (
                _user_data_dir,
                resolve_provider_db_path,
            )
            from omnicode.llm.provider_registry import (
                get_provider_registry,
                reset_provider_registry,
            )
            from omnicode.llm.provider_selection import (
                get_provider_selection_store,
                reset_provider_selection_store,
            )
            # Reset module-level singletons so a working-directory switch
            # actually picks up the new (or shared) DB instead of reusing the
            # one created on the previous boot.
            reset_provider_registry()
            reset_provider_selection_store()

            # One-time migration: if the user previously had a project-local
            # ``<wd>/.data/providers.db`` but no user-level one, copy it up so
            # their existing API keys are immediately available across all
            # projects.  Subsequent edits go through the user-level DB.
            try:
                import shutil as _shutil
                from pathlib import Path as _P
                user_db = _user_data_dir() / "providers.db"
                project_db = _P(working_dir) / ".data" / "providers.db"
                if project_db.exists() and not user_db.exists():
                    user_db.parent.mkdir(parents=True, exist_ok=True)
                    _shutil.copy2(project_db, user_db)
                    # Also copy the encryption key so existing rows can be
                    # decrypted with the same SecretBox.
                    project_key = project_db.with_name("providers.key")
                    user_key = user_db.with_name("providers.key")
                    if project_key.exists() and not user_key.exists():
                        _shutil.copy2(project_key, user_key)
                    logger.info(
                        "🔁 Migrated provider DB %s → %s (user-level shared store)",
                        project_db, user_db,
                    )
            except Exception as exc:
                logger.warning("Provider DB migration skipped: %s", exc)

            provider_db = resolve_provider_db_path(working_dir)
            logger.info(f"📦 Provider registry DB: {provider_db}")
            get_provider_registry(provider_db)
            # Selections (role → provider mapping) live next to the registry.
            from pathlib import Path as _Path
            selections_db = str(_Path(provider_db).with_name("selections.db"))
            get_provider_selection_store(selections_db)
            llm_router = LLMRouter()
            set_llm_router(llm_router)
            logger.info("✅ LLM Router (Model Gateway) initialized")

        # Initialize AST Parser (Tree-sitter)
        ast_parser = UnifiedASTParser()
        set_ast_parser(ast_parser)
        logger.info("✅ Tree-sitter Unified AST Parser initialized")

        # Initialize semantic/hybrid search engine
        search_engine = SemanticSearchEngine(working_dir)
        await search_engine.initialize()
        set_search_engine(search_engine)
        logger.info("✅ Semantic/hybrid search engine initialized")

        # Initialize PatchManager — the safe-edit layer used by both
        # WritePipeline and EditPipeline. Snapshots + EditSession +
        # rollback live here. Wiring it into the pipelines means every
        # LLM-driven write goes through the project's safety contract,
        # not raw `open(..., "w")`.
        from omnicode_core.edit.patch import PatchManager
        patch_manager = PatchManager(working_dir)
        logger.info("✅ PatchManager initialized (snapshot + rollback layer)")

        if features.llm_router:
            # WritePipeline / EditPipeline both build an LLMRouter in
            # their constructors, so they only make sense when the LLM
            # stack is enabled.  Core safe-edit (PatchManager) above
            # already works without them via /patch/* endpoints.
            write_pipeline = WritePipeline(search_engine, patch_manager=patch_manager)
            set_write_pipeline(write_pipeline)
            logger.info("✅ Write pipeline initialized")

            edit_pipeline = EditPipeline(write_pipeline, patch_manager=patch_manager)
            set_edit_pipeline(edit_pipeline)
            logger.info("✅ Edit pipeline initialized")
        else:
            logger.info(
                "⏭️  Write/Edit pipelines skipped (LLM disabled). "
                "Use /patch/preview + /patch/apply for safe core edits."
            )

        # Initialize memory manager.  Deployment state belongs under
        # OMNICODE_STATE_DIR when configured; avoid writing service metadata
        # into a clean repository checkout.
        from pathlib import Path as _StatePath

        state_dir = os.environ.get("OMNICODE_STATE_DIR", "").strip()
        memory_dir = (
            _StatePath(state_dir).expanduser() / "memory"
            if state_dir
            else _StatePath(working_dir) / ".data"
        )
        memory_manager = MemoryManager(str(memory_dir))
        await memory_manager.initialize()
        set_memory_manager(memory_manager)
        logger.info("✅ Memory manager initialized")

        # Initialize git manager
        try:
            logger.info(f"🔍 Initializing GitManager with WORKING_DIR: {working_dir}")
            git_manager = GitManager(working_dir)
            await git_manager.initialize_codebase_repo()
            set_git_manager(git_manager)
            logger.info("✅ Git manager initialized")
        except Exception as e:
            logger.warning(f"⚠️ Git manager initialization failed: {e}")
            set_git_manager(None)

        # Initialize project manager
        project_manager = ProjectStructureManager(working_dir)
        set_project_manager(project_manager)
        logger.info("✅ Project structure manager initialized")

        # Initialize directory lister
        directory_lister = DirectoryLister(working_dir)
        set_directory_lister(directory_lister)
        logger.info("✅ Directory lister initialized")

        logger.info("🚀 All services initialized successfully")

    except Exception as e:
        logger.error(f"❌ Failed to initialize services: {e}")
        raise


async def shutdown_services() -> None:
    """Shutdown all application services"""
    logger.info("🛑 FastAPI Server shutting down...")
    # Add any cleanup logic here if needed


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager

    Handles initialization on startup and cleanup on shutdown
    """
    # Startup
    await initialize_services()

    yield

    # Shutdown
    await shutdown_services()


async def reinitialize_services(new_working_dir: str) -> None:
    """
    Reinitialize all services with new working directory

    Args:
        new_working_dir: New working directory path

    Raises:
        Exception: If reinitialization fails
    """
    from core.dependencies import (
        get_memory_manager,
        get_search_engine,
    )

    logger.info(f"🔄 Reinitializing services with working directory: {new_working_dir}")

    # Store old instances for cleanup
    old_search_engine = get_search_engine()
    old_memory_manager = get_memory_manager()

    try:
        # Update settings
        settings = get_settings()
        settings.update_working_directory(new_working_dir)

        # Initialize all services with new directory
        await initialize_services()

        logger.info("🎉 All services reinitialized successfully")

        # Cleanup old instances if needed
        if old_search_engine:
            try:
                # Add any cleanup logic here if needed
                pass
            except Exception as e:
                logger.warning(f"Warning during old search engine cleanup: {e}")

        if old_memory_manager:
            try:
                # Add any cleanup logic here if needed
                pass
            except Exception as e:
                logger.warning(f"Warning during old memory manager cleanup: {e}")

        return True

    except Exception as e:
        logger.error(f"❌ Failed to reinitialize services: {e}")

        # Try to restore old settings if possible
        if old_search_engine:
            set_search_engine(old_search_engine)
        if old_memory_manager:
            set_memory_manager(old_memory_manager)

        raise
