import uvicorn
from loguru import logger
import os

from app.config import config

if __name__ == "__main__":
    host = os.getenv("HOST", config.listen_host)
    port = int(os.getenv("PORT", config.listen_port))
    
    logger.info(
        f"Starting server on {host}:{port}, docs: http://{host}:{port}/docs"
    )
    uvicorn.run(
        app="app.asgi:app",
        host=host,
        port=port,
        reload=config.reload_debug,
        log_level="warning",
    )
