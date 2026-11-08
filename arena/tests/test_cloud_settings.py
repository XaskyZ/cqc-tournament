import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from config.database import database_config


class CloudDatabaseSettingsTests(SimpleTestCase):
    def test_pooler_settings_and_password_are_parsed_safely(self):
        result = database_config({'DATABASE_URL': 'postgresql://preview:p%40ss@pooler.example:6543/postgres?sslmode=disable'}, '/unused', True)
        self.assertEqual(result['PASSWORD'], 'p@ss')
        self.assertEqual(result['PORT'], 6543)
        self.assertEqual(result['CONN_MAX_AGE'], 0)
        self.assertTrue(result['DISABLE_SERVER_SIDE_CURSORS'])
        self.assertEqual(result['OPTIONS']['sslmode'], 'require')
        self.assertIsNone(result['OPTIONS']['prepare_threshold'])
        self.assertNotIn('transaction_mode', result['OPTIONS'])

    def test_vercel_cannot_silently_use_ephemeral_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesMessage(RuntimeError, 'persistent PostgreSQL'):
                database_config({'VERCEL':'1'}, directory, True)
            self.assertFalse((Path(directory)/'.local').exists())

    def test_non_postgres_and_malformed_urls_do_not_leak_secrets(self):
        for url in ('sqlite:///secret.sqlite3', 'bogus://user:secret@host/db', 'postgresql://user:secret@[broken'):
            with self.subTest(url=url), self.assertRaises(RuntimeError) as error:
                database_config({'DATABASE_URL':url}, '/unused', False)
            self.assertNotIn('secret', str(error.exception))

    def test_local_demo_still_uses_separate_database(self):
        with tempfile.TemporaryDirectory() as directory:
            result=database_config({}, directory, True)
            self.assertEqual(Path(result['NAME']).name, 'tournament-demo.sqlite3')
            self.assertEqual(result['OPTIONS']['transaction_mode'], 'IMMEDIATE')
            with self.assertRaisesMessage(RuntimeError, 'separate database'):
                database_config({'DATABASE_PATH':'tournament.sqlite3'}, directory, True)
