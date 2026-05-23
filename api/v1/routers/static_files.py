"""
Static file serving for templates and components.

We serve all dashboard assets with strong "no-cache" headers so that updates
to JS / HTML / CSS take effect on a simple page reload, without users having
to manually clear their browser cache. This used to silently break features
(e.g. the providers section) when an outdated `routes.js` was served from
the browser cache after the bundle had moved on.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter(tags=["static"])


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@router.get("/")
async def serve_dashboard():
    """Serve the main dashboard index.html"""
    return FileResponse("templates/index.html", headers=_NO_CACHE_HEADERS)


@router.get("/templates/components/{file_path:path}")
async def serve_component(file_path: str):
    """Serve HTML component files"""
    component_path = Path("templates/components") / file_path

    if component_path.exists() and component_path.suffix == ".html":
        return FileResponse(component_path, headers=_NO_CACHE_HEADERS)

    return HTMLResponse(content="<div>Component not found</div>", status_code=404)


@router.get("/static/js/{file_path:path}")
async def serve_javascript(file_path: str):
    """Serve JavaScript files"""
    js_path = Path("templates/static/js") / file_path

    if js_path.exists() and js_path.suffix == ".js":
        return FileResponse(
            js_path,
            media_type="application/javascript",
            headers=_NO_CACHE_HEADERS,
        )

    return HTMLResponse(content="// File not found", status_code=404)


@router.get("/static/css/{file_path:path}")
async def serve_css(file_path: str):
    """Serve CSS files"""
    css_path = Path("templates/static/css") / file_path

    if css_path.exists() and css_path.suffix == ".css":
        return FileResponse(
            css_path,
            media_type="text/css",
            headers=_NO_CACHE_HEADERS,
        )

    return HTMLResponse(content="/* File not found */", status_code=404)
