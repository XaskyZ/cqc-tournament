'use strict';
// Match forms preserve their own position without retaining entered private data.
const matchScrollKey = 'cqc.match.scroll';
try {
  const saved = JSON.parse(sessionStorage.getItem(matchScrollKey) || 'null');
  sessionStorage.removeItem(matchScrollKey);
  if (saved && saved.path === location.pathname && Date.now() - saved.at < 60000) {
    requestAnimationFrame(() => window.scrollTo(0, Number(saved.y) || 0));
  }
} catch (_) {}
document.querySelectorAll('form[data-match-scroll]').forEach((form) => {
  form.addEventListener('submit', () => {
    try { sessionStorage.setItem(matchScrollKey, JSON.stringify({path: location.pathname, y: window.scrollY, at: Date.now()})); } catch (_) {}
  });
  const reportKind = form.querySelector('select[name="kind"]');
  if (reportKind) {
    const updateScoreFields = () => {
      const needsScore = reportKind.value === 'result';
      ['reported_score_a', 'reported_score_b'].forEach((name) => {
        const field = form.elements.namedItem(name);
        if (!field) return;
        field.disabled = !needsScore;
        field.required = needsScore;
        const wrapper = field.closest('.field');
        if (wrapper) wrapper.hidden = !needsScore;
      });
    };
    reportKind.addEventListener('change', updateScoreFields);
    updateScoreFields();
  }
});
