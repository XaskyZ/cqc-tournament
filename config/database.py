"""Database settings shared by local SQLite and hosted PostgreSQL."""
from pathlib import Path

import dj_database_url


def database_config(environ, base_dir, demo):
    url = environ.get('DATABASE_URL', '').strip()
    if url:
        try:
            config = dj_database_url.parse(url, conn_max_age=0, conn_health_checks=True)
        except Exception:
            raise RuntimeError('Invalid DATABASE_URL configuration.') from None
        if config['ENGINE'] != 'django.db.backends.postgresql':
            raise RuntimeError('DATABASE_URL must use PostgreSQL.')
        config['DISABLE_SERVER_SIDE_CURSORS'] = True
        config.setdefault('OPTIONS', {}).update({
            'sslmode': 'require', 'connect_timeout': 10, 'prepare_threshold': None,
        })
        return config
    if environ.get('VERCEL') == '1':
        raise RuntimeError('Vercel requires a persistent PostgreSQL DATABASE_URL.')
    local = Path(base_dir) / '.local'
    local.mkdir(exist_ok=True)
    name = environ.get('DATABASE_PATH', str(local / ('tournament-demo.sqlite3' if demo else 'tournament.sqlite3')))
    if demo and Path(name).name == 'tournament.sqlite3':
        raise RuntimeError('Demo mode must use a separate database.')
    return {'ENGINE': 'django.db.backends.sqlite3', 'NAME': name,
            'OPTIONS': {'timeout': 20, 'transaction_mode': 'IMMEDIATE'}}
