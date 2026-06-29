const REVIEW_KEY = 'miswitch_reviews';
const REVIEW_STATUSES = [
  { key: 'new', label: 'New' },
  { key: 'reviewed', label: 'Reviewed' },
  { key: 'follow-up', label: 'Follow-up' },
  { key: 'ignored', label: 'Ignored' },
];
const REVIEW_LABELS = ['no-name', 'transfer-failed', 'low-confidence', 'vip', 'spam'];

function readReviews() {
  try {
    return JSON.parse(localStorage.getItem(REVIEW_KEY) || '{}');
  } catch {
    return {};
  }
}

function writeReviews(data) {
  localStorage.setItem(REVIEW_KEY, JSON.stringify(data));
}

function effectiveReview(id, fallback = 'new') {
  return readReviews()[id]?.status || fallback;
}

function decorateReviewPills(root = document) {
  root.querySelectorAll('[data-review-id]').forEach((el) => {
    const id = el.dataset.reviewId;
    const status = effectiveReview(id, el.dataset.reviewDefault || 'new');
    el.textContent = REVIEW_STATUSES.find((s) => s.key === status)?.label || status;
    el.className = `pill review-${status.replace('/', '-')}`;
  });
  root.querySelectorAll('[data-row-id]').forEach((row) => {
    const status = effectiveReview(row.dataset.rowId, row.dataset.reviewDefault || 'new');
    const flagged = row.dataset.flagged === '1' || status === 'follow-up';
    row.classList.toggle('flagged', flagged);
    const flag = row.querySelector('[data-row-flag]');
    if (flag) flag.style.visibility = flagged ? 'visible' : 'hidden';
  });
}

function initReviewPanel() {
  const panel = document.querySelector('[data-review-panel]');
  if (!panel) return;

  const insightId = panel.dataset.insightId;
  const reviews = readReviews();
  const saved = reviews[insightId] || { status: 'new', labels: [] };
  const statusGrid = panel.querySelector('[data-review-status-grid]');
  const labelGrid = panel.querySelector('[data-review-label-grid]');
  const note = panel.querySelector('[data-review-note]');

  const persist = (next) => {
    reviews[insightId] = next;
    writeReviews(reviews);
    decorateReviewPills();
    if (note) note.textContent = `persisted locally · ${new Date().toLocaleString()}`;
  };

  if (statusGrid) {
    statusGrid.innerHTML = '';
    REVIEW_STATUSES.forEach((item) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `review-option${saved.status === item.key ? ` active-${item.key}` : ''}`;
      btn.textContent = item.label;
      btn.addEventListener('click', () => {
        persist({ ...saved, status: item.key });
        statusGrid.querySelectorAll('.review-option').forEach((el) => {
          el.className = 'review-option';
        });
        btn.className = `review-option active-${item.key}`;
      });
      statusGrid.appendChild(btn);
    });
  }

  if (labelGrid) {
    labelGrid.innerHTML = '';
    REVIEW_LABELS.forEach((label) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `label-chip${saved.labels?.includes(label) ? ' active' : ''}`;
      btn.textContent = label;
      btn.addEventListener('click', () => {
        const labels = new Set(saved.labels || []);
        if (labels.has(label)) labels.delete(label);
        else labels.add(label);
        const next = { ...saved, labels: [...labels] };
        persist(next);
        btn.classList.toggle('active');
        saved.labels = next.labels;
      });
      labelGrid.appendChild(btn);
    });
  }
}

function initClientFilter() {
  const root = document.querySelector('[data-client-filter]');
  if (!root) return;

  const input = root.querySelector('[data-filter-input]');
  const items = Array.from(root.querySelectorAll('[data-filter-item]'));
  const groups = Array.from(root.querySelectorAll('[data-segment-group]'));

  const apply = () => {
    const needle = (input?.value || '').trim().toLowerCase();
    const activeFilters = {};
    groups.forEach((group) => {
      const active = group.querySelector('.segment-btn.active');
      if (active && active.dataset.value && active.dataset.value !== 'all') {
        activeFilters[group.dataset.segment] = active.dataset.value;
      }
    });

    items.forEach((item) => {
      const haystack = (item.dataset.filterText || '').toLowerCase();
      const matchesQuery = !needle || haystack.includes(needle);
      const matchesChannel = !activeFilters.channel || item.dataset.channel === activeFilters.channel;
      const matchesRes = !activeFilters.resolution || item.dataset.resolution === activeFilters.resolution;
      const matchesSent = !activeFilters.sentiment || item.dataset.sentiment === activeFilters.sentiment;
      item.hidden = !(matchesQuery && matchesChannel && matchesRes && matchesSent);
    });
  };

  input?.addEventListener('input', apply);

  root.querySelectorAll('.segment-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const group = btn.closest('[data-segment-group]');
      group.querySelectorAll('.segment-btn').forEach((el) => el.classList.remove('active'));
      btn.classList.add('active');
      apply();
    });
  });

  apply();
}

document.addEventListener('click', async (event) => {
  const copyButton = event.target.closest('[data-copy-target]');
  if (copyButton) {
    const target = document.getElementById(copyButton.dataset.copyTarget);
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.innerText);
      const old = copyButton.textContent;
      copyButton.textContent = 'Copied';
      setTimeout(() => { copyButton.textContent = old; }, 1200);
    } catch (err) {
      console.warn('Clipboard copy failed', err);
    }
    return;
  }

  const toggleButton = event.target.closest('[data-toggle-target]');
  if (toggleButton) {
    const target = document.getElementById(toggleButton.dataset.toggleTarget);
    if (!target) return;
    target.hidden = !target.hidden;
    toggleButton.textContent = target.hidden ? 'Show JSON' : 'Hide JSON';
  }
});

document.addEventListener('DOMContentLoaded', () => {
  initReviewPanel();
  initClientFilter();
  decorateReviewPills();
});
