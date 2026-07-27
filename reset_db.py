import sqlite3
conn = sqlite3.connect('data/praxis.db')
conn.execute("UPDATE media SET thumb=NULL, enriched=0 WHERE type='game'")
conn.commit()
conn.close()
