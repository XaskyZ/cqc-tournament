from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
path = root / '.env'
text = path.read_text(encoding='utf-8') if path.exists() else (root / '.env.example').read_text(encoding='utf-8')
lines = text.splitlines()
if not any(line.startswith('DJANGO_SECRET_KEY=') and line.split('=', 1)[1] for line in lines):
    lines = [line for line in lines if not line.startswith('DJANGO_SECRET_KEY=')]
    lines.append('DJANGO_SECRET_KEY=' + secrets.token_urlsafe(64))
path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
print('Local configuration ready; credentials were not printed.')
