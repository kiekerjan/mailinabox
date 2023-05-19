#!/usr/bin/python3

from utils import load_environment

def add_cols_sqlite():
	import sqlite3
	
	env = load_environment()
	
	con = sqlite3.connect('file:' + os.path.join(env["STORAGE_ROOT"], "mail/users.sqlite") + '?mode=rw', uri=True)

	cur = con.cursor()

	tbl_mod = 'users'
	
	cols_to_add = ['imap_allowed' 'pop_allowed' 'smtp_allowed']

	existing_cols = [fields[1] for fields in con.execute(f"PRAGMA table_info('{}')".format(tbl_mod)).fetchall()]

	for col_add in cols_to_add:
		# check if the column you want to add is in the list
		if col_add in existing_cols:
			pass # do nothing
		else:
			# alter table
			# default is true (=1) allowed
			cur.execute(f"ALTER TABLE {} ADD COLUMN {} INTEGER NOT NULL DEFAULT 1".format(tbl_mod, col_add))
			con.commit()
	
	con.close()
