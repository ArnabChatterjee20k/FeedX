from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from scout.logger import get_logger

logger = get_logger("API")

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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app
