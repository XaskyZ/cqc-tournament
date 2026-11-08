'use strict';
const demoBanner = document.querySelector('[data-demo-banner]');
if (demoBanner) {
  const preferenceKey = 'cqc.demo-banner.collapsed';
  try { demoBanner.open = localStorage.getItem(preferenceKey) !== '1'; } catch (_) {}
  demoBanner.addEventListener('toggle', () => {
    try { localStorage.setItem(preferenceKey, demoBanner.open ? '0' : '1'); } catch (_) {}
  });
}
document.querySelectorAll('.copy-link').forEach((button) => {
  button.addEventListener('click', async () => {
    const path = button.dataset.copyPath;
    if (!path) return;
    const url = new URL(path, window.location.origin).href;
    const announcement = document.querySelector('.copy-announcement');
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(url);
      button.textContent = 'Ссылка скопирована ✓';
      if (announcement) announcement.textContent = 'Личная ссылка участника скопирована.';
      window.setTimeout(() => { button.textContent = 'Скопировать ссылку'; }, 3500);
    } catch (_) {
      const existing = button.parentElement.querySelector('.copy-fallback');
      if (existing) { existing.focus(); existing.select(); return; }
      const field = document.createElement('input');
      field.className = 'copy-fallback';
      field.type = 'text';
      field.value = url;
      field.readOnly = true;
      field.setAttribute('aria-label', 'Личная ссылка участника — скопируйте вручную');
      button.parentElement.appendChild(field);
      field.focus();
      field.select();
      if (announcement) announcement.textContent = 'Скопируйте выделенную ссылку вручную.';
    }
  });
});

const liveStatus = document.querySelector('[data-live-status-url]');
if (liveStatus) {
  const statusUrl = new URL(liveStatus.dataset.liveStatusUrl, window.location.origin);
  const initialVersion = liveStatus.dataset.liveStatusVersion;
  let polling = false;
  if (statusUrl.origin === window.location.origin && initialVersion) {
    window.setInterval(async () => {
      const active = document.activeElement;
      if (polling || document.hidden || (active && active.matches('input, textarea, select, [contenteditable="true"]'))) return;
      polling = true;
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(statusUrl.href, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' } });
        if (response.redirected) { window.location.reload(); return; }
        if (!response.ok) return;
        const status = await response.json();
        if (typeof status.version === 'string' && status.version !== initialVersion) window.location.reload();
      } catch (_) {
        // A temporary connection failure leaves the current state visible.
      } finally {
        window.clearTimeout(timeout);
        polling = false;
      }
    }, 15000);
  }
}

const telegramStatus = document.querySelector('[data-telegram-status-url]');
if (telegramStatus) {
  const statusUrl = new URL(telegramStatus.dataset.telegramStatusUrl, window.location.origin);
  const initialState = telegramStatus.dataset.telegramState;
  const statusMessage = document.querySelector('[data-telegram-poll-message]');
  let polling = false;
  if (statusUrl.origin === window.location.origin && initialState === 'pending') {
    window.setInterval(async () => {
      if (polling || document.hidden) return;
      polling = true;
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(statusUrl.href, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal, headers: { Accept: 'application/json' } });
        if (!response.ok || response.redirected) throw new Error('Status unavailable');
        const payload = await response.json();
        const status = payload.auth || payload;
        if (typeof status.state === 'string' && status.state !== initialState) {
          window.location.reload();
          return;
        }
        if (statusMessage) statusMessage.textContent = {
          bot_opened: 'Бот открыт. Отправьте код с этой страницы в ответ на его сообщение.',
          code_verified: 'Код принят. Нажмите кнопку подтверждения в Telegram, затем вернитесь сюда.',
        }[status.progress] || 'Ожидаем подтверждение в Telegram…';
      } catch (_) {
        if (statusMessage) statusMessage.textContent = 'Статус пока не обновился. Проверим снова автоматически.';
      } finally {
        window.clearTimeout(timeout);
        polling = false;
      }
    }, 4000);
  }
}

const gameAccountEditor = document.querySelector('[data-game-account-editor]');
if (gameAccountEditor) {
  document.documentElement.classList.add('has-game-account-editor');
  let savingGameAccount = false;
  const holdEditorHeight = () => {
    const height = Math.ceil(gameAccountEditor.getBoundingClientRect().height);
    const previous = parseFloat(gameAccountEditor.style.minHeight) || 0;
    gameAccountEditor.style.minHeight = `${Math.max(previous, height)}px`;
  };
  gameAccountEditor.addEventListener('input', (event) => {
    const field = event.target;
    if (!(field instanceof HTMLInputElement)) return;
    const form = field.closest('[data-game-account-form]');
    if (!form || !['game_id', 'server_id', 'nickname'].includes(field.name)) return;
    holdEditorHeight();
    form.querySelectorAll('[data-account-saved]').forEach((item) => { item.hidden = true; });
    const confirmation = form.elements.namedItem('accept_identity');
    if (confirmation) confirmation.checked = false;
    if (form.dataset.gameAccountApi === 'true') {
      form.querySelectorAll('[data-account-result], [data-account-confirm]').forEach((item) => { item.hidden = true; });
      const receipt = form.elements.namedItem('lookup_token');
      const nickname = form.elements.namedItem('nickname');
      if (receipt) receipt.value = '';
      if (nickname) nickname.value = '';
    } else {
      const saveButton = form.querySelector('[value="save"]');
      if (saveButton) { saveButton.disabled = false; saveButton.textContent = 'Сохранить аккаунт ✓'; }
    }
    const feedback = form.querySelector('[data-game-account-feedback]');
    if (feedback) feedback.textContent = '';
  });
  gameAccountEditor.addEventListener('submit', async (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches('[data-game-account-form]')) return;
    event.preventDefault();
    if (savingGameAccount) return;
    const action = event.submitter?.value || (form.dataset.gameAccountApi === 'true' ? 'lookup' : 'save');
    const endpoint = new URL(form.getAttribute('action') || window.location.href, window.location.href);
    if (endpoint.origin !== window.location.origin) return;
    const data = new FormData(form);
    data.set('action', action);
    const csrfToken = String(data.get('csrfmiddlewaretoken') || '');
    const controls = Array.from(form.elements).filter((control) => 'disabled' in control);
    const disabledStates = controls.map((control) => control.disabled);
    const feedback = form.querySelector('[data-game-account-feedback]');
    const submitScroll = { left: window.scrollX, top: window.scrollY, behavior: 'instant' };
    holdEditorHeight();
    savingGameAccount = true;
    gameAccountEditor.setAttribute('aria-busy', 'true');
    controls.forEach((control) => { control.disabled = true; });
    if (feedback) feedback.textContent = action === 'lookup' ? 'Ищем аккаунт…' : 'Сохраняем профиль…';
    window.scrollTo(submitScroll);
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(endpoint.href, {
        method: 'POST', body: data, credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: { Accept: 'application/json', 'X-CSRFToken': csrfToken },
      });
      if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('Session or response unavailable');
      }
      const result = await response.json();
      if (typeof result.html !== 'string' || !result.html.trim()) throw new Error('Invalid response');
      const scrollPosition = { left: window.scrollX, top: window.scrollY, behavior: 'instant' };
      holdEditorHeight();
      gameAccountEditor.innerHTML = result.html;
      const newFeedback = gameAccountEditor.querySelector('[data-game-account-feedback]');
      if (newFeedback) {
        newFeedback.textContent = result.saved ? 'Профиль сохранён.' : result.ok ? 'Аккаунт найден. Проверьте ник перед сохранением.' : 'Проверьте данные и сообщения об ошибках выше.';
      }
      window.scrollTo(scrollPosition);
    } catch (error) {
      const message = gameAccountEditor.querySelector('[data-game-account-feedback]');
      if (message) {
        message.textContent = error.name === 'AbortError'
          ? 'Сервис не ответил вовремя. Данные остались в форме — попробуйте ещё раз.'
          : 'Не удалось обновить профиль. Проверьте соединение и повторите попытку. Если ошибка сохраняется, обновите страницу и войдите снова.';
        message.setAttribute('role', 'alert');
      }
    } finally {
      window.clearTimeout(timeout);
      controls.forEach((control, index) => { if (control.isConnected) control.disabled = disabledStates[index]; });
      gameAccountEditor.removeAttribute('aria-busy');
      savingGameAccount = false;
    }
  });
}
