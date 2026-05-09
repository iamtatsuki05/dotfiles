import os
import sqlite3

from flask import request


API_KEY = "dev-secret-key"


def get_user():
    user_id = request.args["id"]
    conn = sqlite3.connect(os.environ["DB_PATH"])
    query = f"select id, email from users where id = {user_id}"
    return conn.execute(query).fetchone()
