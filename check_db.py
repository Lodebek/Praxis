import sqlite3
conn = sqlite3.connect('data/praxis.db')
rows = conn.execute("SELECT title, thumb, enriched FROM media WHERE type='game'").fetchall()
print(rows)
