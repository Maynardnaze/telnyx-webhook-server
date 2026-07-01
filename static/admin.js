const REVIEW_STATUSES = [
  { key: 'new', label: 'New' },
  { key: 'reviewed', label: 'Reviewed' },
  { key: 'follow-up', label: 'Follow-up' },
  { key: 'ignored', label: 'Ignored' },
];
const REVIEW_LABELS = ['no-name', 'transfer-failed', 'low-confidence', 'vip', 'spam'];

function initReviewPanel() {
  const panel = document.querySelector('[data-review-panel]');
  if (!panel) return;

  const insightId = panel.dataset.insightId;
  let labels;
  try {
    labels = JSON.parse(panel.dataset.reviewLabels || '[]');
  } catch {
    labels = [];
  }
  const state = { status: panel.dataset.reviewStatus || 'new', labels };
  const statusGrid = panel.querySelector('[data-review-status-grid]');
  const labelGrid = panel.querySelector('[data-review-label-grid]');
  const noteStatus = panel.querySelector('[data-review-note]');
  const noteInput = panel.querySelector('[data-review-note-input]');

  const setNoteStatus = (text, isError = false) => {
    if (!noteStatus) return;
    noteStatus.textContent = text;
    noteStatus.classList.toggle('error-text', isError);
  };

  const persist = async () => {
    setNoteStatus('saving…');
    try {
      const response = await fetch(`/admin/api/reviews/${encodeURIComponent(insightId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ status: state.status, labels: state.labels, note: noteInput?.value || '' }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const saved = await response.json();
      setNoteStatus(saved.updated_at ? `saved · ${new Date(saved.updated_at).toLocaleString()}` : 'not reviewed yet');
    } catch (err) {
      setNoteStatus(`save failed: ${err.message || err}`, true);
    }
  };

  if (statusGrid) {
    statusGrid.innerHTML = '';
    REVIEW_STATUSES.forEach((item) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `review-option${state.status === item.key ? ` active-${item.key}` : ''}`;
      btn.textContent = item.label;
      btn.addEventListener('click', () => {
        state.status = item.key;
        statusGrid.querySelectorAll('.review-option').forEach((el) => {
          el.className = 'review-option';
        });
        btn.className = `review-option active-${item.key}`;
        persist();
      });
      statusGrid.appendChild(btn);
    });
  }

  if (labelGrid) {
    labelGrid.innerHTML = '';
    REVIEW_LABELS.forEach((label) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `label-chip${state.labels.includes(label) ? ' active' : ''}`;
      btn.textContent = label;
      btn.addEventListener('click', () => {
        const next = new Set(state.labels);
        if (next.has(label)) next.delete(label);
        else next.add(label);
        state.labels = [...next];
        btn.classList.toggle('active');
        persist();
      });
      labelGrid.appendChild(btn);
    });
  }

  noteInput?.addEventListener('blur', () => {
    if (noteInput.value.trim() !== (panel.dataset.reviewNote || '').trim()) {
      panel.dataset.reviewNote = noteInput.value;
      persist();
    }
  });
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

function initAssistantNameLoader() {
  const loader = document.querySelector('[data-assistant-name-loader]');
  if (!loader) return;

  const endpoint = loader.dataset.endpoint || '/admin/api/assistant-names?refresh=true';
  const status = loader.querySelector('[data-assistant-name-status]');
  const refresh = loader.querySelector('[data-assistant-name-refresh]');
  const cards = Array.from(document.querySelectorAll('[data-assistant-card][data-assistant-id]'));

  const setStatus = (text, isError = false) => {
    if (!status) return;
    status.textContent = text;
    status.classList.toggle('error-text', isError);
  };

  const load = async () => {
    setStatus('Loading live Telnyx names…');
    try {
      const response = await fetch(endpoint, { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      const names = payload.names || {};
      let updated = 0;
      cards.forEach((card) => {
        const assistantId = card.dataset.assistantId;
        const liveName = names[assistantId];
        const target = card.querySelector('[data-assistant-name]');
        if (liveName && target && target.textContent !== liveName) {
          target.textContent = liveName;
          card.dataset.assistantNameSource = 'telnyx-api';
          updated += 1;
        }
      });
      if (payload.status?.ok) {
        setStatus(`Loaded ${payload.count || Object.keys(names).length} names from Telnyx API · updated ${updated} visible card${updated === 1 ? '' : 's'}`);
      } else {
        setStatus(`Lookup failed: ${payload.status?.reason || 'unknown error'}`, true);
      }
    } catch (err) {
      setStatus(`Lookup failed: ${err.message || err}`, true);
    }
  };

  refresh?.addEventListener('click', load);
  load();
}

document.addEventListener('DOMContentLoaded', () => {
  initReviewPanel();
  initClientFilter();
  initAssistantNameLoader();
});
