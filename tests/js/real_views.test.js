'use strict';
// The viewer on REAL exported views: export_view.build_view over oracle demo runs (no model), written as the .js data
// files the browser loads (view_to_js) and evaluated here the way a <script> tag would.
//   pattern     the shippable test pattern, testpattern.make_test_pattern(drift=False)
//   modec       reference type C: no reference, so no items; AI nodes carry neither item_id nor gutter
//   aimapsonly  one model family, no judge: no items, no judge in run.models
//   tryit       the drifting dev pattern, static/dev/data/tryit.js (already on disk)
// tests/real_views.py builds the first three (about 12 s); set SS_REAL_VIEWS_DIR to reuse a directory it filled.
// Asserted for each: no console error, every AI node drawn exactly once, and the "official pathway" gloss shown once.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const vm = require('vm');
const { spawnSync } = require('child_process');
const { load, ROOT } = require('./harness');

const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));
const clone = (o) => JSON.parse(JSON.stringify(o));

function readData(file, name) {
  const sandbox = { window: {} };
  sandbox.window.window = sandbox.window;
  vm.runInNewContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: path.basename(file) });
  return clone(sandbox.window.SS_DATA[name]);
}

const VIEWS = (() => {
  let dir = process.env.SS_REAL_VIEWS_DIR;
  let made = null;
  if (!dir) {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ss-tests-realviews-'));
    made = dir;
    const r = spawnSync('python3', ['-m', 'tests.real_views', dir], { cwd: ROOT, encoding: 'utf8' });
    if (r.status !== 0) throw new Error('tests.real_views failed:\n' + r.stdout + r.stderr);
  }
  const out = {};
  for (const name of ['pattern', 'modec', 'aimapsonly']) out[name] = readData(path.join(dir, name + '.js'), name);
  out.tryit = readData(path.join(ROOT, 'second_scribe', 'app', 'static', 'dev', 'data', 'tryit.js'), 'tryit');
  if (made) {                                  // remove only the files this run made, then the empty directory
    for (const name of ['pattern', 'modec', 'aimapsonly']) fs.unlinkSync(path.join(made, name + '.js'));
    fs.rmdirSync(made);
  }
  return out;
})();

function boot(hash) {
  const env = load();
  const doc = env.window.document;
  const make = doc.createElement;
  doc.createElement = (tag) => {
    const n = make(tag);
    n.listeners = {};
    n.addEventListener = (type, fn) => { (n.listeners[type] = n.listeners[type] || []).push(fn); };
    n.focus = () => {};
    let text = n.textContent;
    Object.defineProperty(n, 'textContent', {
      get: () => text,
      set: (v) => { text = v; if (v === '') n.children.length = 0; },
    });
    return n;
  };
  env.window.location.hash = hash || '';
  return env;
}
function walk(node, fn) { fn(node); (node.children || []).forEach((c) => walk(c, fn)); }
function find(root, pred) { const out = []; walk(root, (n) => { if (pred(n)) out.push(n); }); return out; }
const attr = (n, k) => (n.attributes ? n.attributes[k] : undefined);
const byAttr = (root, k, v) => find(root, (n) => attr(n, k) !== undefined && (v === undefined || attr(n, k) === v));
function allText(root) {
  const out = [];
  walk(root, (n) => { if (n.textContent) out.push(n.textContent); });
  return out.join('\n');
}
function count(text, needle) { return text.split(needle).length - 1; }
function section(root, name) {
  const hit = byAttr(root, 'data-section', name);
  assert.strictEqual(hit.length, 1, 'one ' + name + ' section');
  return hit[0];
}
function mount(env, view, opts) {
  const root = env.window.document.createElement('div');
  env.SS.viewer.mount(root, clone(view), opts);
  return root;
}
const aiNodes = (map, rung) => ((map.lanes.ai[rung] || {}).nodes || []);
const dups = (list) => list.filter((x, i) => list.indexOf(x) !== i);

// ---------------------------------------------------------------- the shippable pattern (items, fates, no gutter)

test('real pattern: mounts with no console error, gloss once, in every view', () => {
  for (const view of ['lanes', 'overlay', 'columns']) {
    const env = boot('#view=' + view);
    const root = mount(env, VIEWS.pattern, {});
    assert.deepStrictEqual(env.errors, [], view);
    // item 20 (23 Oct): the viewer draws the lanes whatever view the hash names, so the gloss is there once
    assert.strictEqual(count(allText(root), EN['lane.no_interview_gloss']), 1, view);
    assert.strictEqual(count(allText(root), EN['band.test_pattern']), 1, view);
    assert.strictEqual(count(allText(root), EN['band.synthetic']), 1, view);
  }
});

test('real pattern: every AI node is drawn exactly once, at every map and rung', () => {
  const env = boot('#view=lanes');
  const root = mount(env, VIEWS.pattern, {});
  for (const map of VIEWS.pattern.maps) {
    for (const rung of VIEWS.pattern.rungs) {
      env.SS.state.set({ map: map.pid, rung });
      const boxes = find(section(root, 'views'), (n) => n.tagName === 'BUTTON' && attr(n, 'data-lane') === 'ai').map((n) => attr(n, 'data-item-id'));
      // the AI lane has one box for each reference step (a step the AI never drew is a dotted box), so every AI node
      // that matched a step is drawn once, in that step's box, and no box is drawn twice
      const want = map.items.map((i) => i.item_id);
      assert.deepStrictEqual(dups(boxes), [], map.pid + ' ' + rung + ': an AI box twice');
      assert.deepStrictEqual(boxes.slice().sort(), want.slice().sort(), map.pid + ' ' + rung);
      const nodeItems = aiNodes(map, rung).map((n) => n.item_id).filter(Boolean);
      assert.deepStrictEqual(dups(nodeItems), [], 'two AI nodes on one step would share a box');
      nodeItems.forEach((id) => assert.ok(boxes.includes(id), id));
      // and the AI nodes that matched no step (the gutter) are plain boxes of the AI lane, once each (none in this file)
      assert.strictEqual(aiNodes(map, rung).filter((n) => n.gutter).length, 0);
    }
  }
  assert.deepStrictEqual(env.errors, []);
});

test('real pattern: the AI lane draws every AI-only node once (the drift file has none, so one is added)', () => {
  // item 20 (23 Oct): the overlay is gone from the page, so the AI's own extra boxes are drawn in the AI lane, on a row
  // below its steps, as plain boxes that say they are not on the endorsed map (item 18)
  const v = clone(VIEWS.pattern);
  v.maps[0].lanes.ai.rung2.nodes.push({ node_id: 'extra', label: 'Only the AI drew this', phase: null, gutter: true });
  const env = boot('#view=overlay&rung=rung2');
  const root = mount(env, v, {});
  const g = find(root, (n) => /\bss-plain-node\b/.test(n.className || '') && attr(n, 'data-lane') === 'ai');
  assert.strictEqual(g.length, 1);
  assert.strictEqual(g[0].tagName, 'DIV');
  assert.strictEqual(attr(g[0], 'data-status'), EN['box.ai.only'].replace('{ref}', EN['box.ref_word']));
  assert.ok(allText(root).includes(EN['lane.no_phase']), 'the node with no phase has its column');
  assert.strictEqual(find(root, (n) => /\bss-gutter-node\b/.test(n.className || '')).length, 0, 'no overlay gutter');
  assert.deepStrictEqual(env.errors, []);
});

// Task 15 records the code hash, so a real view names its code version (sha256) and never says "not recorded".
function checkCodeVersion(t, view) {
  assert.match(view.run.code_sha256, /^[0-9a-f]{64}$/, 'the real file carries code_sha256');
  assert.strictEqual(count(t, view.run.code_sha256), 1);
  assert.strictEqual(count(t, EN['drawer.run.code_none']), 0);
}

test('real pattern: the drawer shows the log check and the code version the run recorded', () => {
  const view = VIEWS.pattern;
  const item = view.maps[0].items[0].item_id;
  const env = boot('#map=' + view.maps[0].pid + '&rung=rung2&item=' + item);
  const root = mount(env, view, {});
  const t = allText(section(root, 'drawer'));
  assert.ok(t.includes(EN['drawer.verify.passed']), 'verify status');
  checkCodeVersion(t, view);
  assert.ok(!/0{16}/.test(t), 'no run of zeros');
  assert.deepStrictEqual(env.errors, []);
});

// ---------------------------------------------------------------- the no-interview lane on real views

// Every node of the no-interview map is drawn once in the lanes (a button over its step when it matches one, a plain
// box in its phase otherwise). Item 20 (23 Oct): a hash that names the columns or the overlay still draws the lanes.
function checkScriptLane(name, view) {
  const scriptNodes = (map) => ((map.lanes.script || {}).nodes || []);
  assert.ok(view.maps.some((m) => m.items.length && scriptNodes(m).some((n) => n.item_id)), name + ': some node sits on a step');
  assert.ok(view.maps.some((m) => m.items.length && scriptNodes(m).some((n) => n.gutter === true)), name + ': some node is in the gutter');
  for (const v of ['lanes', 'columns', 'overlay']) {
    const env = boot('#view=' + v);
    const root = mount(env, view, {});
    for (const map of view.maps) {
      for (const rung of [view.rungs[0], view.rungs[view.rungs.length - 1]]) {
        env.SS.state.set({ map: map.pid, rung });
        const panel = section(root, 'views');
        const drawn = find(panel, (n) => attr(n, 'data-lane') === 'script');
        const ids = drawn.map((n) => attr(n, 'data-node-id'));
        const where = name + ' ' + v + ' ' + map.pid + ' ' + rung;
        assert.deepStrictEqual(dups(ids), [], where + ': a node drawn twice');
        assert.deepStrictEqual(ids.slice().sort(), scriptNodes(map).map((n) => n.node_id).sort(), where + ': every node once');
        // a node on a step is that step's button, at the step's place (x in the lanes, row in the columns)
        const refBtn = (id) => find(panel, (n) => n.tagName === 'BUTTON' && attr(n, 'data-lane') === 'reference' && attr(n, 'data-item-id') === id)[0];
        for (const n of scriptNodes(map)) {
          const box = drawn.filter((d) => attr(d, 'data-node-id') === n.node_id)[0];
          if (!n.item_id) { assert.strictEqual(attr(box, 'data-item-id'), undefined, where + ' ' + n.node_id); continue; }
          if (attr(box, 'data-item-id') === undefined) continue;     // a second node on the same step: a plain box
          assert.strictEqual(attr(box, 'data-item-id'), n.item_id, where + ' ' + n.node_id);
          const ref = refBtn(n.item_id);
          const pos = 'left';
          assert.strictEqual(box.style[pos], ref.style[pos], where + ' ' + n.node_id + ': at its step');
        }
      }
    }
    assert.deepStrictEqual(env.errors, [], name + ' ' + v);
  }
}

test('real pattern: every no-interview node is drawn exactly once, over its step or in its phase', () => {
  checkScriptLane('pattern', VIEWS.pattern);
});

test('real tryit (drift): every no-interview node is drawn exactly once, over its step or in its phase', () => {
  checkScriptLane('tryit', VIEWS.tryit);
});

test('real tryit (drift): a label the judge cache reused says so in the evidence, with the record it came from', () => {
  const view = VIEWS.tryit;
  let hit = null;
  for (const m of view.maps) {
    for (const i of m.items) {
      for (const r of Object.keys(i.evidence || {})) {
        const e = i.evidence[r];
        if (e.request_id && !e.request_id.includes(m.pid + '__')) assert.strictEqual(e.judge_cached, true, m.pid + ' ' + i.item_id + ' ' + r);
        if (!hit && e.judge_cached) hit = { m, i, r, e };
      }
    }
  }
  assert.ok(hit, 'a reused label');
  const env = boot('#map=' + hit.m.pid + '&rung=' + hit.r + '&item=' + hit.i.item_id);
  const root = mount(env, view, {});
  const log = byAttr(section(root, 'drawer'), 'data-step', 'log')[0];
  const t = allText(log);
  assert.ok(t.includes(EN['drawer.step.log']));
  const want = EN['drawer.log_value_cached'].replace('{request_id}', hit.e.request_id).replace('{record_index}', String(hit.e.record_index));
  assert.ok(t.includes(want), t);
  assert.deepStrictEqual(env.errors, []);
});

// ---------------------------------------------------------------- the drifting dev pattern

test('real tryit (drift): leaked and flipped steps are drawn, every AI box once, no console error', () => {
  const view = VIEWS.tryit;
  assert.strictEqual(view.dev_only, true);
  const env = boot('#view=lanes');
  const root = mount(env, view, {});
  let sawLeak = false;
  for (const rung of view.rungs) {
    env.SS.state.set({ rung });
    const boxes = find(section(root, 'views'), (n) => n.tagName === 'BUTTON' && attr(n, 'data-lane') === 'ai');
    const ids = boxes.map((n) => attr(n, 'data-item-id'));
    assert.deepStrictEqual(dups(ids), [], rung);
    assert.strictEqual(ids.length, view.maps[0].items.length, rung);
    if (boxes.some((n) => /ss-shape-leaked/.test(n.className))) sawLeak = true;
  }
  assert.ok(sawLeak, 'a leaked step shows somewhere in the first map');
  for (const v of ['overlay', 'columns']) env.SS.state.set({ view: v });
  assert.deepStrictEqual(env.errors, []);
});

// ---------------------------------------------------------------- mode C

test('real mode C: AI nodes with neither item_id nor gutter are drawn as plain boxes, once each', () => {
  const view = VIEWS.modec;
  const probe = view.maps[0].lanes.ai.rung2.nodes[0];
  assert.ok(probe && probe.item_id === undefined && probe.gutter === undefined, 'the real file has neither field');
  for (const v of ['lanes', 'overlay', 'columns']) {
    const env = boot('#view=' + v);
    const root = mount(env, view, {});
    for (const map of view.maps) {
      for (const rung of view.rungs) {
        env.SS.state.set({ map: map.pid, rung });
        const plain = byAttr(root, 'data-plain', 'true');
        const ai = plain.filter((n) => attr(n, 'data-lane') === 'ai');
        const ids = ai.map((n) => attr(n, 'data-node-id'));
        assert.deepStrictEqual(ids, aiNodes(map, rung).map((n) => n.node_id), v + ' ' + map.pid + ' ' + rung + ': in their own order, once each');
        assert.ok(ids.length > 0);
        ai.forEach((n) => assert.strictEqual(attr(n, 'data-rung'), rung));
        const script = plain.filter((n) => attr(n, 'data-lane') === 'script').map((n) => attr(n, 'data-node-id'));
        assert.deepStrictEqual(script, map.lanes.script.nodes.map((n) => n.node_id));
      }
    }
    const t = allText(root);
    assert.strictEqual(count(t, EN['lane.no_interview_gloss']), 1, v);
    assert.ok(!t.includes(EN['views.gutter.title']), v + ': no "only the AI drew" in mode C');
    assert.ok(!t.includes(EN['lane.empty']), v);
    assert.strictEqual(find(root, (n) => /ss-item-glyph|ss-item-tag/.test(n.className || '')).length, 0, v + ': no fate marks');
    assert.deepStrictEqual(env.errors, [], v);
  }
});

test('real mode C: the drawer shows the log check and the code version line', () => {
  const env = boot('');
  const root = mount(env, VIEWS.modec, {});
  const t = allText(section(root, 'drawer'));
  assert.ok(t.includes(EN['drawer.verify.passed']));
  checkCodeVersion(t, VIEWS.modec);
  assert.strictEqual(count(allText(root), EN['band.synthetic']), 1);
});

// ---------------------------------------------------------------- AI maps only (no judge)

test('real AI maps only: the AI maps are drawn at every rung under "needs a judge", once each', () => {
  const view = VIEWS.aimapsonly;
  assert.strictEqual(view.run.models.judge, undefined);
  assert.strictEqual(view.states.needs_judge, true);
  const env = boot('');
  const root = mount(env, view, {});
  const views = section(root, 'views');
  assert.ok(allText(views).includes(EN['common.needs_judge']));
  for (const map of view.maps) {
    env.SS.state.set({ map: map.pid });
    const ai = byAttr(views, 'data-plain', 'true').filter((n) => attr(n, 'data-lane') === 'ai');
    const got = ai.map((n) => attr(n, 'data-rung') + '/' + attr(n, 'data-node-id'));
    const want = [];
    view.rungs.forEach((r) => aiNodes(map, r).forEach((n) => want.push(r + '/' + n.node_id)));
    assert.deepStrictEqual(got, want, map.pid + ': every AI node of every rung, once, in its own order');
    assert.deepStrictEqual(dups(got), []);
  }
  const t = allText(views);
  assert.strictEqual(count(t, EN['lane.no_interview_gloss']), 1);
  assert.ok(!t.includes(EN['views.gutter.title']));
  assert.ok(!t.includes(EN['ledger.selector.legend']), 'no rung selector: the rungs are all shown');
  assert.strictEqual(find(views, (n) => n.tagName === 'BUTTON' && attr(n, 'data-item-id') !== undefined).length, 0);
  // the map picker is there, one button per map
  assert.strictEqual(byAttr(views, 'data-map').length, view.maps.length);
  // no judge needed for the run's own facts
  const d = allText(section(root, 'drawer'));
  assert.ok(d.includes(EN['common.needs_judge']));
  assert.ok(d.includes(EN['drawer.verify.passed']));
  checkCodeVersion(d, view);
  assert.deepStrictEqual(env.errors, []);
});

test('real AI maps only: choosing a map in the picker changes the maps drawn', () => {
  const view = VIEWS.aimapsonly;
  const env = boot('');
  const root = mount(env, view, {});
  const second = byAttr(section(root, 'views'), 'data-map').filter((n) => attr(n, 'data-map') === view.maps[1].pid)[0];
  (second.listeners.click || []).forEach((f) => f({}));
  assert.strictEqual(env.SS.state.get().map, view.maps[1].pid);
  const got = byAttr(root, 'data-plain', 'true').filter((n) => attr(n, 'data-lane') === 'ai' && attr(n, 'data-rung') === 'rung2')
    .map((n) => attr(n, 'data-node-id'));
  assert.deepStrictEqual(got, aiNodes(view.maps[1], 'rung2').map((n) => n.node_id));
});
