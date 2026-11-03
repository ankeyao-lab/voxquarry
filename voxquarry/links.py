"""Explicit product-to-source links. Never infer identity from similar names."""
import sqlite3

class ProductSources:
    def __init__(self,path):
        self.path=path
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS product_sources (product_id TEXT NOT NULL, job_id TEXT NOT NULL, PRIMARY KEY(product_id,job_id))')
    def connect(self):return sqlite3.connect(self.path)
    def add(self,product_id,job_id):
        with self.connect() as db:db.execute('INSERT OR IGNORE INTO product_sources VALUES (?,?)',(product_id,job_id))
    def sources(self,product_id):
        with self.connect() as db:return [r[0] for r in db.execute('SELECT job_id FROM product_sources WHERE product_id=? ORDER BY job_id',(product_id,))]
