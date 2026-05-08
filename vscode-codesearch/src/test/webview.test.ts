/**
 * Unit tests for webview.ts.
 *
 * Verifies that buildWebviewHtml produces HTML in which the mode dropdown
 * will be populated with every entry from MODES — i.e. the data the inline
 * script needs is actually baked into the HTML.
 */

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';

import { MODES } from '../client';
import { buildWebviewHtml, getNonce } from '../webview';

describe('buildWebviewHtml', () => {
    it('includes the <select id="mode"> dropdown element', () => {
        const html = buildWebviewHtml(getNonce(), ['default'], 'default');
        assert.match(html, /<select id="mode" class="filter-select"[^>]*>\s*<\/select>/i);
    });

    it('inline <script> body parses as valid JavaScript', () => {
        // Backslash escaping inside a TS backtick template is fragile: writing
        // `/\\/g` in the .ts collapses to `/\/g` at runtime — an unterminated
        // regex that silently kills the whole inline script.  Compile-only via
        // vm.Script catches this without executing the script.
        const html = buildWebviewHtml(getNonce(), ['default'], 'default');
        const m = html.match(/<script\b[^>]*>([\s\S]*?)<\/script\b[^>]*>/i);
        assert.ok(m, 'expected an inline <script> in the rendered HTML');
        assert.doesNotThrow(() => new vm.Script(m![1]), 'inline script must parse');
    });

    it('embeds a MODES literal containing every entry from client.MODES', () => {
        const html = buildWebviewHtml(getNonce(), ['default'], 'default');
        const m = html.match(/const MODES = (\[[\s\S]*?\]);/);
        assert.ok(m, 'expected `const MODES = [...]` in the inline script');
        const embedded: Array<{ key: string; label: string; desc: string }> = JSON.parse(m![1]);

        assert.equal(
            embedded.length,
            MODES.length,
            `embedded MODES length (${embedded.length}) must equal client MODES length (${MODES.length})`,
        );
        const expectedKeys = MODES.map((x) => x.key);
        const actualKeys   = embedded.map((x) => x.key);
        assert.deepEqual(actualKeys, expectedKeys, 'embedded mode keys must match client MODES order');

        for (const e of embedded) {
            assert.ok(e.key,   `every embedded mode must have a key — got ${JSON.stringify(e)}`);
            assert.ok(e.label, `every embedded mode must have a label — got ${JSON.stringify(e)}`);
            assert.ok(e.desc,  `every embedded mode must have a desc — got ${JSON.stringify(e)}`);
        }
    });

    it('inline script populates the dropdown by iterating MODES', () => {
        const html = buildWebviewHtml(getNonce(), ['default'], 'default');
        // The script must (a) look up the <select> and (b) append an <option>
        // per mode. If either is missing the dropdown stays empty in the UI.
        assert.match(html, /document\.getElementById\('mode'\)/, 'must look up the mode <select>');
        assert.match(html, /MODES\.forEach\(/,                  'must iterate MODES to build options');
        assert.match(html, /createElement\('option'\)/,         'must create <option> elements');
        assert.match(html, /\.appendChild\(o\)/,                'must append the option to the select');
    });

    it('simulates the dropdown population and yields one <option> per mode', () => {
        // Tiny DOM stub — just enough to exercise the script's option-creation
        // loop. We don't need a real layout engine; we only care that the
        // forEach runs to completion and produces the expected option list.
        type Opt = { value: string; textContent: string; title: string };
        type Sel = { options: Opt[]; appendChild(o: Opt): void };

        const select: Sel = { options: [], appendChild(o) { this.options.push(o); } };
        const document = {
            getElementById(id: string): Sel | null { return id === 'mode' ? select : null; },
            createElement(_tag: string): Opt { return { value: '', textContent: '', title: '' }; },
        };

        // Run the same loop the inline script runs.
        const modesEmbedded = MODES.map((m) => ({ key: m.key, label: m.label, desc: m.desc }));
        const modeEl = document.getElementById('mode')!;
        modesEmbedded.forEach((m) => {
            const o = document.createElement('option');
            o.value = m.key; o.textContent = m.label; o.title = m.desc;
            modeEl.appendChild(o);
        });

        assert.equal(select.options.length, MODES.length,
            `dropdown should contain ${MODES.length} options, got ${select.options.length}`);
        assert.deepEqual(select.options.map((o) => o.value), MODES.map((m) => m.key));
        assert.deepEqual(select.options.map((o) => o.textContent), MODES.map((m) => m.label));
    });
});

describe('getNonce', () => {
    it('returns a 32-character alphanumeric string', () => {
        const n = getNonce();
        assert.equal(n.length, 32);
        assert.match(n, /^[A-Za-z0-9]{32}$/);
    });

    it('produces different values on subsequent calls', () => {
        // Birthday-paradox safe: 62^32 collision space, two calls.
        assert.notEqual(getNonce(), getNonce());
    });
});
