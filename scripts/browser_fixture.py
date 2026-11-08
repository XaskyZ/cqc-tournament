"""Explicitly synthetic UI fixture; refuses the real tournament database."""
import os
import sys
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from arena.models import Tournament

if Path(settings.DATABASES['default']['NAME']).name != 'browser-qa.sqlite3':
    raise SystemExit('This fixture may only run against browser-qa.sqlite3')
start = timezone.now() + timedelta(days=7)
Tournament.objects.get_or_create(pk=1, defaults={'title': 'ДЕМО: тест интерфейса CQC', 'description': 'ДЕМОНСТРАЦИОННАЯ ТЕСТОВАЯ БАЗА. Все участники, результаты и оплаты здесь вымышлены. Не оплачивать.', 'rules': 'ДЕМОНСТРАЦИЯ. Только тестирование интерфейса, реальные оплаты не проводятся.', 'max_teams': 2, 'roster_size': 5, 'reserve_limit': 1, 'reserves_pay': True, 'best_of': 3, 'final_best_of': 5, 'grand_final_reset': True, 'starts_at': start, 'end_registration': start - timedelta(days=1), 'event_date': start.date(), 'timezone_name': 'UTC', 'registration_open': True, 'terms_confirmed': True, 'prize_pool': 0, 'prize_note': 'ДЕМОНСТРАЦИЯ: призов нет.'})
users = get_user_model()
if not users.objects.filter(username='demo-organizer').exists():
    users.objects.create_superuser('demo-organizer', password='DemoOnly-2026-Testing')
print('Synthetic browser fixture ready. No Telegram worker is started for this database.')
