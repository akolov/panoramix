import sqlite3

def get_sqlite3_cursor(name):
    db = sqlite3.connect(name)
    cur = db.cursor()
    return cur
