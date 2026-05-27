"""
FastAPI Codebase Manager - Main Entry Point
Clean, maintainable entry point with modular router architecture
"""

import logging
import argparse
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from core import lifespan, get_settings
from api.v1.routers import all_routers

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """
    Create and configure FastAPI application

    Returns:
        Configured FastAPI app instance
    """
    import os
    from omnicode_core.config.features import get_features

    settings = get_settings()
    features = get_features()

    # Headless mode check — OMNICODE_WEB_CONSOLE=false disables static file serving
    if os.environ.get("OMNICODE_WEB_CONSOLE", "").lower() == "false":
        features.headless()

    # Create FastAPI app
    app = FastAPI(
        title=settings.API_TITLE,
        description=settings.API_DESCRIPTION,
        version=settings.API_VERSION,
        lifespan=lifespan,
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=settings.CORS_CREDENTIALS,
        allow_methods=settings.CORS_METHODS,
        allow_headers=settings.CORS_HEADERS,
    )

    # Optional API key auth (no-op when OMNICODE_API_KEY is empty)
    from core.auth_middleware import install as install_auth
    install_auth(app, settings.OMNICODE_API_KEY)

    # Register routers — skip static file / web console routers in headless mode
    for router in all_routers:
        # In headless mode, skip the static file router (serves templates/*)
        if not features.web_console and getattr(router, "tags", None) == ["static"]:
            logger.info("⏭️  Skipping static file router (headless mode)")
            continue
        app.include_router(router)
        logger.info(f"✅ Registered router: {router.tags}")

    # Exception handlers
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc: HTTPException):
        """Handle HTTP exceptions"""
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.detail,
                "success": False,
            },
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request, exc: Exception):
        """Handle general exceptions"""
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "success": False,
            },
        )

    logger.info("🚀 FastAPI app created successfully")
    logger.info(f"📁 Working directory: {settings.WORKING_DIR}")

    return app


# Create app instance
app = create_app()


def main():
    """Main entry point for running the server"""
    parser = argparse.ArgumentParser(description="FastAPI Codebase Manager Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=6789, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    parser.add_argument("working_dir", nargs="?", help="Working directory path")

    args = parser.parse_args()

    # Update working directory if provided
    if args.working_dir:
        import os

        settings = get_settings()
        settings.update_working_directory(os.path.abspath(args.working_dir))
        logger.info(f"📁 Using working directory: {settings.WORKING_DIR}")
    else:
        settings = get_settings()
        logger.warning(
            f"No working directory specified. Using default: {settings.WORKING_DIR}"
        )
        logger.warning(
            "For proper git operations, specify: python main.py /path/to/project"
        )

    logger.info(f"🚀 Starting FastAPI server on {args.host}:{args.port}")

    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
