// SPEC-27 test harness: a minimal, faithful DOM host so the generated id shim
// can be executed by a real JS engine without a browser or jsdom.
//
// It models exactly the three behaviours the shim depends on:
//   1. document.currentScript -> the <script> element being executed, and
//      .parentElement -> the live section it was appended to.
//   2. Element.querySelector, which throws SyntaxError on an invalid CSS
//      identifier (real browsers reject `#12:30` etc).
//   3. document.getElementById, the document-global original the shim
//      captures by reference and must never call by name (recursion).
//   4. The classic-script currentScript lifecycle: non-null for the
//      SYNCHRONOUS prefix of a section's script only. A probe may return
//      a Promise (deferred lookup); by the time its continuation runs,
//      currentScript is null again -- exactly as in a real document.
//
// The caller passes a scenario; we print a JSON array of per-section
// results. Deferred (promise-returning) probes are awaited before the
// next section starts and before the aggregate JSON is printed.

'use strict';

const SCENARIO = JSON.parse(process.env.DC_SCENARIO || '{}');
const SHIM = SCENARIO.shim;

// ---------------------------------------------------------------- DOM model
class FakeEl {
  constructor(tag, attrs, parent) {
    this.tagName = tag.toUpperCase();
    this.attrs = attrs || {};
    this.children = [];
    this.parentElement = parent || null;
  }
  appendChild(c) { c.parentElement = this; this.children.push(c); return c; }
  getAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attrs, n)
    ? this.attrs[n] : null; }
  setAttribute(n, v) { this.attrs[n] = String(v); }
  // Depth-first descendants, excluding self.
  *descendants() {
    for (const c of this.children) { yield c; yield* c.descendants(); }
  }
  // Real browsers throw SyntaxError for a selector that is not a valid
  // CSS identifier; the shim relies on catching that and using the
  // [id="..."] attribute form instead.
  querySelector(sel) {
    let m;
    if ((m = /^#(.+)$/.exec(sel))) {
      const raw = m[1];
      if (!/^-?[_a-zA-Z][_a-zA-Z0-9-]*$/.test(raw)) {
        throw new SyntaxError("'#' + raw + ' is not a valid selector");
      }
      for (const d of this.descendants()) {
        if (d.getAttribute('id') === raw) return d;
      }
      return null;
    }
    if ((m = /^\[id="([\s\S]*)"\]$/.exec(sel))) {
      const raw = m[1].replace(/\\(.)/g, '$1');
      for (const d of this.descendants()) {
        if (d.getAttribute('id') === raw) return d;
      }
      return null;
    }
    throw new SyntaxError('unsupported selector: ' + sel);
  }
}

const body = new FakeEl('body', {}, null);
const document = {
  currentScript: null,
  body: body,
  getElementById(id) {
    for (const d of body.descendants()) {
      if (d.getAttribute('id') === id) return d;
    }
    return null;
  },
  createElement(tag) { return new FakeEl(tag, {}, null); },
};
// CSS.escape is present in every target engine (WebKit); model the escaping of
// the characters that matter for id selectors.
document.defaultView = null;
const window = {
  document: document,
  CSS: {
    escape(s) {
      return String(s).replace(/([!"#$%&'()*+,./:;<=>?@[\\\]^`{|}~])/g, '\\$1');
    },
  },
};
document.defaultView = window;
globalThis.document = document;
globalThis.window = window;
globalThis.CSS = window.CSS;

// ------------------------------------------------------------- build DOM
// Body-level nodes live OUTSIDE every live section, so a section-scoped
// lookup must miss them and fall through to the document original.
function buildBodyChildren(specs) {
  const made = [];
  for (const spec of specs || []) {
    const el = new FakeEl(spec.tag || 'div', spec.attrs || {}, body);
    body.appendChild(el);
    made.push(el);
  }
  return made;
}

function buildSection(spec) {
  const sec = new FakeEl('div', { class: 'live-section', id: spec.id }, body);
  body.appendChild(sec);
  for (const child of spec.children) {
    const el = new FakeEl(child.tag || 'div', child.attrs || {}, sec);
    sec.appendChild(el);
  }
  const script = new FakeEl('script', {}, sec);
  sec.appendChild(script);
  return { sec: sec, script: script };
}

// --------------------------------------------------------- run the probe
// Async: a probe may return a Promise (deferred lookup). We await it so the
// continuation runs BEFORE the aggregate JSON is printed, while
// document.currentScript is already null again -- the classic-script
// lifecycle the id shim must respect.
async function runProbe(fnSrc) {
  const out = [];
  // Body-level nodes exist OUTSIDE every live section, so a section-scoped
  // lookup must miss them and fall through to the document original.
  buildBodyChildren(SCENARIO.bodyChildren);
  for (const spec of SCENARIO.sections) {
    const built = buildSection(spec);
    // The shim is installed once per window, exactly as in a real document.
    if (!window.__dcIdShimInstalled) {
      document.currentScript = built.script;
      // eslint-disable-next-line no-eval
      globalThis.eval(SHIM);
      document.currentScript = null;
    }
    // Now the section's own script runs with currentScript live for its
    // synchronous prefix only; deferred continuations see it cleared.
    let res;
    try {
      document.currentScript = built.script;
      // eslint-disable-next-line no-eval
      res = globalThis.eval('(function(){' + fnSrc + '})()');
    } finally {
      document.currentScript = null;
    }
    if (res && typeof res.then === 'function') {
      res = await res;
    }
    out.push(res === undefined ? null : res);
  }
  return out;
}

runProbe(SCENARIO.probe).then(
  function (result) {
    process.stdout.write(JSON.stringify({ ok: true, result: result }));
  },
  function (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(e && e.message || e) }));
  }
);
