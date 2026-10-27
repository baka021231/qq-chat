"""Profile-scoped model preferences and shared, bounded history import."""
import json


class AgentState:
    def __init__(self, db, legacy_profile, profile_types=None):
        self.db, self.legacy_profile = db, legacy_profile
        db.executescript('''
            CREATE TABLE IF NOT EXISTS agent_sessions (
                conversation TEXT, profile TEXT, session_id TEXT,
                PRIMARY KEY (conversation, profile));
            CREATE TABLE IF NOT EXISTS old_agent_sessions (
                conversation TEXT, profile TEXT, session_id TEXT, time REAL);
            CREATE TABLE IF NOT EXISTS profile_models (
                conversation TEXT, profile TEXT, model TEXT, effort TEXT,
                PRIMARY KEY (conversation, profile));
            CREATE TABLE IF NOT EXISTS agent_metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS agent_profile_types (profile TEXT PRIMARY KEY, type TEXT);
        ''')
        db.execute("INSERT OR IGNORE INTO agent_metadata VALUES ('legacy_codex_profile', ?)", (legacy_profile,))
        self.legacy_profile = db.execute("SELECT value FROM agent_metadata WHERE key='legacy_codex_profile'").fetchone()[0]
        types = {self.legacy_profile: 'codex_app', **(profile_types or {})}
        for name, kind in types.items():
            old = db.execute('SELECT type FROM agent_profile_types WHERE profile=?', (name,)).fetchone()
            if old and old[0] != kind:
                db.rollback()
                raise ValueError('Profile type changed; use a new profile name')
            db.execute('INSERT OR IGNORE INTO agent_profile_types VALUES (?, ?)', (name, kind))
        db.execute('INSERT OR IGNORE INTO profile_models '
                   'SELECT conversation, ?, model, effort FROM conversation_models', (self.legacy_profile,))
        db.commit()

    def selection(self, conversation, profile):
        row = self.db.execute('SELECT model, effort FROM profile_models WHERE conversation=? AND profile=?',
                              (conversation, profile)).fetchone()
        if row is None and profile == self.legacy_profile:
            # Keep the legacy Codex table readable during the transition.
            row = self.db.execute('SELECT model, effort FROM conversation_models WHERE conversation=?',
                                  (conversation,)).fetchone()
        return row

    def save(self, conversation, profile, model, effort):
        try:
            self.db.execute('INSERT INTO profile_models VALUES (?, ?, ?, ?) '
                            'ON CONFLICT(conversation, profile) DO UPDATE '
                            'SET model=excluded.model, effort=excluded.effort',
                            (conversation, profile, model, effort))
            if profile == self.legacy_profile:
                self.db.execute('INSERT INTO conversation_models VALUES (?, ?, ?) '
                                'ON CONFLICT(conversation) DO UPDATE '
                                'SET model=excluded.model, effort=excluded.effort',
                                (conversation, model, effort))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


def recent_history(db, conversation):
    rows = db.execute('SELECT role, content FROM history WHERE conversation=? '
                      'ORDER BY rowid DESC LIMIT 20', (conversation,)).fetchall()
    history = []
    for role, content in rows:
        content = content if len(content) <= 1000 else content[:999] + '…'
        candidate = [(role, content), *history]
        if len(json.dumps(candidate, ensure_ascii=False)) > 8000:
            break
        history = candidate
    return history
