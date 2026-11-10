import logging

from dotenv_lite import load_env

load_env()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from app import create_app  # noqa: E402

application = create_app()
app = application
