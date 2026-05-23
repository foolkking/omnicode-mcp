/**
 * Global loading-overlay helpers.
 *
 * Used to be duplicated in templates/components/sections/{working-directory,
 * directory}.html, which meant ANY page that called `showLoading()` before
 * one of those sections had been loaded would crash with
 * "showLoading is not defined".  This was the root cause of the
 * "Refresh failed: showLoading is not defined" error users saw on the
 * dashboard right after starting the server.
 *
 * The overlay element itself is rendered by templates/components/loading.html
 * (loaded via the component loader on app boot).  If it isn't present yet
 * (e.g. the user clicked Refresh before initial render finished), we render
 * a tiny fallback toast instead so the call still succeeds gracefully.
 */
(function () {
    'use strict';

    function _ensureFallback() {
        let el = document.getElementById('loading-overlay-fallback');
        if (el) return el;
        el = document.createElement('div');
        el.id = 'loading-overlay-fallback';
        el.className = 'fixed inset-0 bg-black bg-opacity-30 z-[100] flex items-center justify-center';
        el.style.display = 'none';
        el.innerHTML =
            '<div class="bg-white dark:bg-gray-800 rounded-lg shadow-2xl px-6 py-4 flex items-center space-x-3 text-gray-800 dark:text-gray-100">' +
            '  <i class="fas fa-spinner fa-spin text-indigo-500"></i>' +
            '  <span id="loading-overlay-fallback-text" class="text-sm">Loading…</span>' +
            '</div>';
        document.body.appendChild(el);
        return el;
    }

    function showLoading(message) {
        const text = (message == null || message === '') ? 'Loading…' : String(message);
        // Prefer the rich overlay shipped in components/loading.html.
        const overlay = document.getElementById('loading-overlay');
        if (overlay) {
            const msgEl = overlay.querySelector('[data-loading-message]')
                || document.getElementById('loading-message');
            if (msgEl) msgEl.textContent = text;
            overlay.classList.remove('hidden');
            overlay.style.display = '';
            return;
        }
        // Fallback: render a tiny inline overlay.
        const fb = _ensureFallback();
        const fbText = document.getElementById('loading-overlay-fallback-text');
        if (fbText) fbText.textContent = text;
        fb.style.display = '';
    }

    function hideLoading() {
        const overlay = document.getElementById('loading-overlay');
        if (overlay) {
            overlay.classList.add('hidden');
        }
        const fb = document.getElementById('loading-overlay-fallback');
        if (fb) fb.style.display = 'none';
    }

    // Expose on window so onclick handlers and section scripts that don't
    // import anything still see them.
    window.showLoading = showLoading;
    window.hideLoading = hideLoading;
})();
