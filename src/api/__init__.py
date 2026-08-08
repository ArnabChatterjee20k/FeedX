import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from scout.logger import get_logger
from .models import LoginRequest
from .auth import create_user, AUTH_COOKIE

logger = get_logger("API")

STATIC_DIR = Path(__file__).parent / "static"


def _cors_origins() -> list[str]:
    raw = os.environ.get("FRONTEND_URL", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# @asynccontextmanager
# async def lifespan(app):
#     logger.info(msg="Init database", tag="INIT")
#     if os.environ.get("ENVIRONMENT") == "production":
#         init_database()
#     logger.info(msg="Init database done", tag="INIT")
#     yield
#     logger.info("Shutdown")


def create_api():
    from .routes import router

    load_dotenv()
    # Hack: dont init here because during fastapi dev both watcher/reloader and server reads the create_api and init database would run twice then
    # use a lifespan and run it there and it would be blocking as its async function
    # init_database()
    app = FastAPI()
    # Restrict CORS to the configured frontend origin(s). When FRONTEND_URL names
    # explicit origins we can safely allow credentials; with the wildcard fallback
    # we must not (browsers reject '*' + credentials). openfeed authenticates with a
    # Bearer header, not cookies, so it doesn't need credentials cross-origin anyway.
    origins = _cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=bool(origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.post("/login")
    def login_user(body: LoginRequest, response: Response):
        user = create_user(body.password)
        if user is None:
            raise HTTPException(status_code=401, detail="Wrong password")
        response.set_cookie(
            key=AUTH_COOKIE,
            value=user,
            httponly=True,
            samesite="lax",
            max_age=30 * 24 * 60 * 60,
        )
        # `token` is also returned in the body so cross-origin clients (e.g. the
        # openfeed static site) can store it and send it as a Bearer header — the
        # httponly cookie can't be read by JS and isn't sent cross-site.
        return {"status": "success", "token": user}

    @app.post("/logout")
    def logout_user(response: Response):
        response.delete_cookie(key=AUTH_COOKIE)
        return {"status": "success"}

    app.frontend("/", directory=str(STATIC_DIR), fallback="index.html")

    return app
