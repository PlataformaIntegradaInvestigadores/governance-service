import sqlite3

import psycopg

# Driver failures the API turns into a denial or an unavailable response.
STORAGE_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error, psycopg.Error)
