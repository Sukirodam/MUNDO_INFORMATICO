
import os
import mysql.connector
from urllib.parse import urlparse, unquote
from dotenv import load_dotenv

load_dotenv()


def obtener_conexion():

    database_url = os.getenv("DATABASE_URL")

    # CONEXIÓN PARA ETHIODEPLOY
    if database_url:
        url = urlparse(database_url)

        return mysql.connector.connect(
            host=url.hostname,
            port=url.port or 3306,
            user=unquote(url.username or ""),
            password=unquote(url.password or ""),
            database=url.path.lstrip("/")
        )

    # CONEXIÓN LOCAL PARA APPSERV
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        port=int(os.getenv("DB_PORT", 3306))
    )
