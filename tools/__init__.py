"""Scripts that operate on an existing scoreboard.db.

Two of them, `export_tables` and `coverage`, are also reachable as the CLI's
`export` and `coverage` commands. Both stay runnable on their own, so this
package exists only to make that import possible; nothing here is imported at
CLI load time.
"""
