import sqlite3

def check_db():
    conn = sqlite3.connect('jarvis.db')
    cur = conn.cursor()
    
    cur.execute("PRAGMA table_info(semantic_memories)")
    sm_cols = [r[1] for r in cur.fetchall()]
    print('semantic_memories cols:', sm_cols)
    assert 'subject' in sm_cols, "subject column missing in semantic_memories"
    assert 'project_id' not in sm_cols, "project_id still in semantic_memories"
    
    cur.execute("PRAGMA table_info(episodes)")
    ep_cols = [r[1] for r in cur.fetchall()]
    print('episodes cols:', ep_cols)
    assert 'related_project_id' not in ep_cols, "related_project_id still in episodes"
    
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='projects'")
    has_projects = bool(cur.fetchone())
    print('projects table exists:', has_projects)
    assert not has_projects, "projects table still exists"
    
    print("Database verification passed!")

if __name__ == '__main__':
    check_db()
