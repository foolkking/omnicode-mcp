/**
 * Theme manager — light / dark / system tri-state.
 *
 * - Persists choice to localStorage under `omnicode.theme`.
 * - Tracks system preference via prefers-color-scheme.
 * - Sets `data-theme="dark"` on <html> when dark mode is active.
 * - Dispatches a `themechange` CustomEvent so other components (e.g. the
 *   D3 graph viewer) can re-render their canvas.
 *
 * Globals exposed on window:
 *   window.theme.get()           -> "light" | "dark" | "system"
 *   window.theme.getEffective()  -> "light" | "dark"   (resolves "system")
 *   window.theme.set(mode)       -> persists + applies
 *   window.theme.toggle()        -> cycles light → dark → system → light
 */
(function (root) {
    'use strict';

    var STORAGE_KEY = 'omnicode.theme';
    var VALID = ['light', 'dark', 'system'];

    function readStored() {
        try {
            var v = root.localStorage && root.localStorage.getItem(STORAGE_KEY);
            return VALID.indexOf(v) >= 0 ? v : 'system';
        } catch (e) {
            return 'system';
        }
    }

    function writeStored(value) {
        try {
            if (root.localStorage) root.localStorage.setItem(STORAGE_KEY, value);
        } catch (e) { /* ignore quota / private mode */ }
    }

    function systemPrefersDark() {
        try {
            return root.matchMedia &&
                root.matchMedia('(prefers-color-scheme: dark)').matches;
        } catch (e) {
            return false;
        }
    }

    function effective(mode) {
        if (mode === 'dark' || mode === 'light') return mode;
        return systemPrefersDark() ? 'dark' : 'light';
    }

    function apply(mode) {
        var eff = effective(mode);
        var html = root.document.documentElement;
        if (eff === 'dark') {
            html.setAttribute('data-theme', 'dark');
            html.classList.add('dark');
        } else {
            html.removeAttribute('data-theme');
            html.classList.remove('dark');
        }
        // Notify listeners
        try {
            root.dispatchEvent(new CustomEvent('themechange', {
                detail: { mode: mode, effective: eff }
            }));
        } catch (e) { /* IE-style — won't happen here */ }
    }

    var current = readStored();
    apply(current);

    // React to system preference changes when user picked "system".
    try {
        var mql = root.matchMedia('(prefers-color-scheme: dark)');
        var listener = function () {
            if (readStored() === 'system') apply('system');
        };
        if (mql.addEventListener) mql.addEventListener('change', listener);
        else if (mql.addListener) mql.addListener(listener);
    } catch (e) { /* ignore */ }

    function set(mode) {
        if (VALID.indexOf(mode) < 0) return;
        current = mode;
        writeStored(mode);
        apply(mode);
        // Update any visible theme toggle buttons
        refreshButtons();
    }

    function toggle() {
        var next = current === 'light' ? 'dark'
                 : current === 'dark'  ? 'system'
                 : 'light';
        set(next);
    }

    function refreshButtons() {
        var btns = root.document.querySelectorAll('[data-theme-toggle]');
        var eff = effective(current);
        var labelEn = current === 'system'
            ? 'System'
            : (eff === 'dark' ? 'Dark' : 'Light');
        var labelZh = current === 'system'
            ? '跟随系统'
            : (eff === 'dark' ? '深色' : '浅色');
        var icon = current === 'system'
            ? 'fa-circle-half-stroke'
            : (eff === 'dark' ? 'fa-moon' : 'fa-sun');
        btns.forEach(function (btn) {
            var i = btn.querySelector('i');
            var span = btn.querySelector('[data-theme-label]');
            if (i) i.className = 'fas ' + icon + ' mr-1.5';
            if (span) {
                var lang = (root.i18n && root.i18n.getLang && root.i18n.getLang()) || 'en';
                span.textContent = lang === 'zh' ? labelZh : labelEn;
            }
            btn.title = (root.i18n && root.i18n.t)
                ? root.i18n.t('theme.toggleHint') || 'Click to switch theme'
                : 'Click to switch theme';
        });
    }

    // Public API
    root.theme = {
        get: function () { return current; },
        getEffective: function () { return effective(current); },
        set: set,
        toggle: toggle,
        _refreshButtons: refreshButtons,
    };

    // Refresh button labels once the DOM is ready (sidebar/header may not
    // exist yet when this script first runs).
    if (root.document.readyState === 'loading') {
        root.document.addEventListener('DOMContentLoaded', refreshButtons);
    } else {
        refreshButtons();
    }
    // Also refresh after every section load (componentLoader fires this)
    root.document.addEventListener('sectionLoaded', refreshButtons);
    // And after language toggle so labels update
    root.document.addEventListener('languagechange', refreshButtons);
})(typeof window !== 'undefined' ? window : this);
