"""FastAPI entrypoint — the uvicorn launcher lives here (backend/main.py convention).

Run: `uv sync`, then `python main.py` (or `just api`). Binds 127.0.0.1:8013.
Dev convenience only: `--reload` stays off unless `CAOBUNGA_API_RELOAD` opts in.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from caobunga.api import (
    ALLOWED_HOSTS,
    API_HOST,
    API_PORT,
    UI_ORIGINS,
    api_reload_enabled,
    install_exception_handlers,
    router,
)

app = FastAPI(title="caobunga")
app.add_middleware(
    CORSMiddleware,
    allow_origins=UI_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.include_router(router)
install_exception_handlers(app)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=API_HOST, port=API_PORT, reload=api_reload_enabled())
