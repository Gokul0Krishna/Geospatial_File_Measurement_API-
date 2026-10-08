"""ASGI entrypoint: ``uvicorn app.asgi:app`` / ``gunicorn -k uvicorn.workers.UvicornWorker app.asgi:app``."""

from app.main import create_app

app = create_app()
