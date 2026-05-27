/**
 * Front-end feature flag toggler.
 *
 * Some panels in the Web Console (e.g. AI Session Management) are
 * deprecated but still wired to live REST endpoints. We keep the markup
 * shipped but hide it behind a `data-feature="<flag-name>"` attribute,
 * with a runtime opt-in via either:
 *
 *   * URL: append `?feature=<flag>` to enable for the current tab.
 *   * Persistent: `localStorage["omnicode.feature.<flag>"] = "true"`.
 *
 * Multiple flags can be supplied with `?feature=a,b`.
 *
 * Globals exposed on window:
 *   - `omnicodeFeatures.isEnabled(name)`
 *   - `omnicodeFeatures.applyAll(rootElement?)`  // rerun after dynamic load
 *
 * Re-run `applyAll()` whenever a new section's HTML is injected — see
 * `components/loader.js`.
 */
(function (root) {
    'use strict';

    const STORAGE_PREFIX = 'omnicode.feature.';

    // Keep the camelCase name exposed in localStorage stable; the
    // attribute on the DOM is kebab-case to match HTML conventions.
    function _kebabToCamel(name) {
        return name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    }

    function _readUrlFlags() {
        const params = new URLSearchParams(root.location.search || '');
        const raw = params.get('feature') || '';
        return new Set(
            raw
                .split(',')
                .map((s) => s.trim())
                .filter(Boolean)
        );
    }

    let _urlFlags = _readUrlFlags();

    function isEnabled(name) {
        if (!name) return false;
        if (_urlFlags.has(name)) return true;
        try {
            const v = root.localStorage.getItem(STORAGE_PREFIX + _kebabToCamel(name));
            return v === 'true' || v === '1';
        } catch (_) {
            return false;
        }
    }

    function applyAll(rootEl) {
        const scope = rootEl || root.document;
        const nodes = scope.querySelectorAll('[data-feature]');
        nodes.forEach((el) => {
            const flag = el.getAttribute('data-feature');
            if (!flag) return;
            if (isEnabled(flag)) {
                el.classList.remove('hidden');
            } else {
                el.classList.add('hidden');
            }
        });
    }

    root.omnicodeFeatures = {
        isEnabled,
        applyAll,
        // Convenience helpers usable from the dev console.
        enable(name) {
            try {
                root.localStorage.setItem(
                    STORAGE_PREFIX + _kebabToCamel(name),
                    'true'
                );
            } catch (_) {
                /* ignore */
            }
            applyAll();
        },
        disable(name) {
            try {
                root.localStorage.removeItem(
                    STORAGE_PREFIX + _kebabToCamel(name)
                );
            } catch (_) {
                /* ignore */
            }
            applyAll();
        },
    };

    if (root.document.readyState === 'loading') {
        root.document.addEventListener('DOMContentLoaded', () => applyAll());
    } else {
        applyAll();
    }
})(window);
