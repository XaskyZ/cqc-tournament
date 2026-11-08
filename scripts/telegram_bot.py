"""Telegram setup and private-chat onboarding. Never logs bot credentials.

Payment decisions are deliberately unavailable until the website's payment
service and explicit cashier allowlist are connected.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def config():
    values = {}
    path = ROOT / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip()
    return {**values, **os.environ}


class TelegramError(RuntimeError):
    pass


def cashier_id():
    """Only one explicitly configured positive user ID may be a cashier."""
    value = config().get('TELEGRAM_CASHIER_IDS', '').strip()
    if not value:
        return None
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise TelegramError('Configure exactly one positive Telegram cashier ID')
    return int(value)


def api(method, payload=None, photo=None):
    token = config().get('TELEGRAM_BOT_TOKEN', '')
    if not token:
        raise TelegramError('TELEGRAM_BOT_TOKEN is missing')
    payload = payload or {}
    headers = {'Content-Type': 'application/json'}
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if photo is not None:
        boundary = 'CQC' + uuid.uuid4().hex
        parts = []
        for key, value in payload.items():
            value = json.dumps(value) if not isinstance(value, str) else value
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="avatar"; filename="avatar.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode())
        parts.append(Path(photo).read_bytes())
        parts.append(f'\r\n--{boundary}--\r\n'.encode())
        body = b''.join(parts)
        headers = {'Content-Type': f'multipart/form-data; boundary={boundary}'}
    request = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}', data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        # Exception strings can contain credential-bearing URLs. Never print them.
        raise TelegramError(f'Telegram {method}: HTTP {exc.code}') from None
    except (urllib.error.URLError, TimeoutError):
        raise TelegramError(f'Telegram {method}: network unavailable') from None
    if not result.get('ok'):
        raise TelegramError(f'Telegram {method}: rejected')
    return result['result']


def inspect():
    me = api('getMe')
    webhook = api('getWebhookInfo')
    photos = api('getUserProfilePhotos', {'user_id': me['id'], 'limit': 1})
    print(json.dumps({'username': me['username'], 'name': me['first_name'], 'webhook_configured': bool(webhook.get('url')), 'profile_photo_count': photos['total_count']}, ensure_ascii=False))


def configure():
    api('setMyShortDescription', {'short_description': 'Турниры CQC: команды, регистрация и проверка взносов.'})
    api('setMyDescription', {'description': 'Бот турниров CQC. Назначенный кассир получает заявки и подтверждает взносы после сверки в Kaspi Pay. /pending — платежи на проверке. Участники видят свои статусы на сайте турнира.'})
    api('setMyCommands', {'commands': [
        {'command': 'start', 'description': 'Начать работу'},
        {'command': 'id', 'description': 'Узнать свой Telegram ID'},
        {'command': 'pending', 'description': 'Взносы на проверке (кассир)'},
        {'command': 'help', 'description': 'Как проверять взносы'},
    ]})
    print('Bot profile and commands configured')


def handle(update):
    message = update.get('message')
    if message and message.get('chat', {}).get('type') == 'private':
        command = message.get('text', '').split(' ', 1)[0].split('@', 1)[0]
        if command in ('/start', '/id'):
            user_id = message['from']['id']
            role = ('Ваш аккаунт назначен единственным кассиром.' if user_id == cashier_id() else 'Передайте этот ID организатору для подключения кассира. Само нажатие «Начать» не даёт права подтверждать оплаты.')
            text = f'CQC Tournament\n\nВаш Telegram ID: {user_id}\n\n{role}\n\nПриём заявок на проверку взносов пока не подключён.'
        elif command == '/help':
            text = 'Кассир сверяет поступление в Kaspi Pay и затем подтверждает взнос в боте. Кнопки проверки появятся после подключения сайта. Сейчас можно узнать свой ID командой /id.'
        else:
            return
        api('sendMessage', {'chat_id': message['chat']['id'], 'text': text})
    callback = update.get('callback_query')
    if callback:
        allowed = cashier_id() is not None and callback.get('from', {}).get('id') == cashier_id()
        text = ('Проверка взносов ещё не подключена. Статус не изменён.' if allowed else 'Нет доступа: подтверждать взносы может только назначенный кассир.')
        api('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': text, 'show_alert': True})


def run():
    cashier_id()
    if api('getWebhookInfo').get('url'):
        raise TelegramError('A webhook is configured; refusing to start a second receiver')
    state_dir = ROOT / '.local'
    state_dir.mkdir(exist_ok=True)
    state_path = state_dir / 'telegram-offset.json'
    offset = json.loads(state_path.read_text())['offset'] if state_path.exists() else 0
    print('Bot onboarding is running; payment decisions remain disabled', flush=True)
    while True:
        try:
            updates = api('getUpdates', {'offset': offset, 'timeout': 25, 'allowed_updates': ['message', 'callback_query']})
            for update in updates:
                handle(update)
                offset = update['update_id'] + 1
                temporary = state_path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'offset': offset}), encoding='utf-8')
                temporary.replace(state_path)
        except TelegramError as exc:
            print(str(exc), flush=True)
            time.sleep(5)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['inspect', 'configure', 'run', 'set-photo'])
    parser.add_argument('--photo', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'set-photo':
            if not args.photo or not args.photo.is_file():
                parser.error('--photo must identify an existing JPEG file')
            api('setMyProfilePhoto', {'photo': {'type': 'static', 'photo': 'attach://avatar'}}, photo=args.photo)
            print('Bot profile photo updated')
        else:
            {'inspect': inspect, 'configure': configure, 'run': run}[args.action]()
    except TelegramError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('Bot stopped')
