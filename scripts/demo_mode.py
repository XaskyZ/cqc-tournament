"""Switch the local site between a separate test database and the real draft.

Stop the website and bot with stop.ps1 before running; then run start.ps1.
Existing demo accounts, sessions and teams are preserved when reopening demo.
"""
import argparse
import os
from pathlib import Path
import sqlite3
import sys
from datetime import timedelta
from zoneinfo import ZoneInfo

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
parser = argparse.ArgumentParser()
parser.add_argument('mode', choices=['on', 'off'])
mode = parser.parse_args().mode
real = root / '.local' / 'tournament.sqlite3'
demo = root / '.local' / 'tournament-demo.sqlite3'
created = False
if mode == 'on' and not demo.exists():
    if not real.exists():
        raise SystemExit('Run setup.ps1 first.')
    with sqlite3.connect(real.as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(demo) as target:
        source.backup(target)
    created = True

# Keep the single bot consumer's offset when switching environments, avoiding
# replay of Telegram messages against an older database snapshot.
source_path, target_path = (real, demo) if mode == 'on' else (demo, real)
if not target_path.exists():
    raise SystemExit('Target database does not exist.')
if source_path.exists():
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        offset = source.execute("SELECT value FROM arena_botstate WHERE key='telegram_update_offset'").fetchone()
        existing = target.execute("SELECT value FROM arena_botstate WHERE key='telegram_update_offset'").fetchone()
        if offset:
            value = str(max(int(offset[0]), int(existing[0]) if existing else 0))
            target.execute("INSERT INTO arena_botstate(key,value) VALUES('telegram_update_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))

env = root / '.env'
lines = env.read_text(encoding='utf-8-sig').splitlines()
lines = [line for line in lines if line.split('=', 1)[0].strip() not in ('TOURNAMENT_DEMO_MODE', 'DATABASE_PATH')]
lines.append('TOURNAMENT_DEMO_MODE=' + ('1' if mode == 'on' else '0'))
env.write_text('\n'.join(lines) + '\n', encoding='utf-8')
os.environ['TOURNAMENT_DEMO_MODE'] = '1' if mode == 'on' else '0'
os.environ['DATABASE_PATH'] = str(target_path)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
if mode == 'on':
    from arena.models import Tournament
    from django.utils import timezone
    from arena.services import registration_errors
    tournament = Tournament.objects.order_by('pk').first()
    if created:
        start = timezone.now() + timedelta(days=30)
        tournament.title = 'ТЕСТОВЫЙ ТУРНИР CQC'
        tournament.description = 'Тестовый турнир для проверки сайта. Регистрация открыта. Взносы, матчи и призы демонстрационные; реальные деньги переводить не нужно.'
        tournament.rules = 'ТЕСТОВЫЙ РЕГЛАМЕНТ. 5 основных игроков и до 1 запасного. Тестовый взнос 2 200 ₸ за каждого заявленного игрока. Матчи BO3, финал BO5, двойная сетка с повторным финалом. Это временные условия для проверки интерфейса, а не утверждённые правила реального турнира. Денежных переводов и призов нет.'
        tournament.prize_pool = 0
        tournament.prize_note = 'Тестовый турнир: призов нет.'
        tournament.timezone_name = 'Asia/Qyzylorda'
        tournament.starts_at = start
        tournament.event_date = timezone.localtime(start, ZoneInfo(tournament.timezone_name)).date()
        tournament.end_registration = start - timedelta(days=1)
        tournament.roster_size = 5
        tournament.reserve_limit = 1
        tournament.reserves_pay = True
        tournament.best_of = 3
        tournament.final_best_of = 5
        tournament.grand_final_reset = True
        tournament.terms_confirmed = True
        tournament.registration_open = True
        tournament.save()
    errors = registration_errors(tournament)
    print('DEMO database active; existing login and profile copied on first activation.')
    print('Registration ready:', not errors)
    if errors:
        print('Review demo tournament settings in organizer panel.')
else:
    print('Real draft database active. Demo teams and payments are separate.')
