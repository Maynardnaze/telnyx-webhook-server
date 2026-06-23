document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-copy-target]');
  if (!button) return;
  const target = document.getElementById(button.dataset.copyTarget);
  if (!target) return;
  try {
    await navigator.clipboard.writeText(target.innerText);
    const old = button.textContent;
    button.textContent = 'Copied';
    setTimeout(() => { button.textContent = old; }, 1200);
  } catch (err) {
    console.warn('Clipboard copy failed', err);
  }
});
