'use strict';
// Task 29 (plan V5): the Explain page's six scenes. Loads core.js + site-explain.js into a vm context with a
// stub document (no DOM library), and the case from static/dev/data/explain.js (kept fresh by
// tests/test_explain_data.py, so it equals build_case() of the shipped SYN-OW03 files).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..', '..');
const APP = path.join(ROOT, 'second_scribe', 'app');
const JS_DIR = path.join(APP, 'static', 'js');
const EN = JSON.parse(fs.readFileSync(path.join(APP, 'strings', 'en.json'), 'utf8'));
const CASE_JS = path.join(APP, 'static', 'dev', 'data', 'explain.js');
const TRY_HREF = 'try.html#map=SYN-OW03&rung=rung2';

function makeNode(tag) {
  const node = {
    nodeType: 1, tagName: String(tag).toUpperCase(), children: [], attributes: {}, dataset: {}, style: {},
    textContent: '', hidden: false,
    setAttribute(k, v) { node.attributes[k] = String(v); },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(node.attributes, k) ? node.attributes[k] : null; },
    removeAttribute(k) { delete node.attributes[k]; },
    appendChild(c) { node.children.push(c); return c; },
    addEventListener() {},
  };
  return node;
}

function load() {
  const errors = [];
  const sandbox = {
    console: { error: (...a) => errors.push(a.join(' ')), warn() {}, log() {} },
    document: {
      documentElement: makeNode('html'), body: makeNode('body'), createElement: makeNode,
      createElementNS: (ns, tag) => makeNode(tag),
      createTextNode: (text) => ({ nodeType: 3, textContent: String(text), children: [] }),
      addEventListener() {},
    },
    location: { hash: '' },
    setTimeout, clearTimeout,
    SS_STRINGS: Object.assign({}, EN),
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  for (const f of ['core.js', 'site-explain.js']) {
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, f), 'utf8'), ctx, { filename: f });
  }
  vm.runInContext(fs.readFileSync(CASE_JS, 'utf8'), ctx, { filename: 'explain.js' });
  const kase = JSON.parse(JSON.stringify(sandbox.SS_DATA.explain));
  return { SS: sandbox.SS, win: sandbox, kase, errors };
}

// All visible text under a node (text nodes and textContent), in document order.
function texts(node, out) {
  out = out || [];
  if (!node) return out;
  if (node.textContent && (!node.children || node.children.length === 0)) out.push(node.textContent);
  for (const c of node.children || []) texts(c, out);
  return out;
}
function allText(node) { return texts(node).join(' '); }
function find(node, pred, out) {
  out = out || [];
  if (!node) return out;
  if (pred(node)) out.push(node);
  for (const c of node.children || []) find(c, pred, out);
  return out;
}
const plain = (x) => JSON.parse(JSON.stringify(x));
const sorted = (a) => plain(a).sort();
const hasClass = (name) => (n) => n.attributes && (' ' + (n.attributes['class'] || '') + ' ').indexOf(' ' + name + ' ') >= 0;

const IDS = ['interview', 'endorsed', 'ai', 'no_interview', 'side_by_side', 'own'];

test('six_scenes_in_order', () => {
  const { SS, kase } = load();
  const s = JSON.parse(JSON.stringify(SS.explain.scenes(kase)));
  assert.deepEqual(s.map((x) => x.id), IDS);
  assert.deepEqual(s.map((x) => x.caption_key), IDS.map((id) => 'explain.scene.' + id));
  for (const x of s) {
    assert.ok(Array.isArray(x.frames) && x.frames.length >= 1, x.id + ' has frames');
    assert.equal(typeof EN[x.caption_key], 'string', x.caption_key);
  }
});

test('captions_are_the_v5_story', () => {
  assert.equal(EN['explain.scene.interview'], 'Someone tells their journey in an interview.');
  assert.equal(EN['explain.scene.endorsed'], 'The interviewer draws the map live, and the participant endorses it.');
  assert.equal(EN['explain.scene.ai'], 'The AI reads the transcript, alone, and draws its map.');
  assert.equal(EN['explain.scene.no_interview'],
    'The same model draws the no-interview map: domain, role and phases given, no interview.');
  assert.equal(EN['explain.scene.side_by_side'],
    'The three readings side by side, each measured against the same transcript: what did listening add?');
});

test('scene6_copy_exact', () => {
  const { SS, kase } = load();
  // council 30 (MUST 5): no sentence under 8 words, no "X, never Y"
  assert.equal(EN['explain.scene.own'], 'Run it on your own interviews, on your computer. Labs can pool their profiles, and the interviews stay with each lab.');
  assert.equal(EN['explain.scene.own_note'], 'The app runs on your computer, and by default nothing leaves your machine. If your ethics ' +
    'approval allows a cloud model, you choose where the text goes, and the app records it.');
  const s6 = SS.explain.renderScene(SS.explain.scenes(kase)[5], kase, true);
  const t = allText(s6);
  assert.ok(t.indexOf(EN['explain.scene.own']) >= 0);
  assert.ok(t.indexOf(EN['explain.scene.own_note']) > t.indexOf(EN['explain.scene.own']), 'the note follows the caption');
});

test('no_illustration_caption', () => {
  // the author's item 4 (23 Oct): the caption "Illustration of what the tool measures, not a recorded result" is gone
  const { SS, kase } = load();
  assert.equal('explain.illustration' in EN, false);
  const t = allText(SS.explain.renderAll(kase, { reduced: true })).toLowerCase();
  assert.equal(t.indexOf('illustration'), -1);
  assert.equal(t.indexOf('recorded result'), -1);
  SS.explain.scenes(kase).forEach((x) => {
    assert.equal(find(SS.explain.renderScene(x, kase, true), hasClass('ss-x-illustration')).length, 0, x.id);
  });
});

test('wished_for_tags_and_the_added_sentences', () => {
  // the author's items 3, 5, 6 and 7 (23 Oct)
  const { SS, kase } = load();
  assert.equal(EN['explain.missing_tag'], 'wished for, never came');
  assert.equal(EN['explain.ai_missing_tag'], 'drawn as wished for');  // council 30: one vocabulary
  assert.equal(EN['explain.legend.missing'], 'A service the person wanted and never got: dashed outline');
  const s = SS.explain.scenes(kase);
  const s2 = allText(SS.explain.renderScene(s[1], kase, true));
  assert.ok(s2.indexOf(EN['explain.missing_tag']) >= 0, 'scene 2 tags the wished-for box');
  assert.ok(s2.indexOf(EN['explain.scene.endorsed_note']) > s2.indexOf(EN['explain.scene.endorsed']), 'item 6 note follows the caption');
  assert.ok(allText(SS.explain.renderScene(s[2], kase, true)).indexOf(EN['explain.ai_missing_tag']) >= 0, 'scene 3 tag');
  const s5 = SS.explain.renderScene(s[4], kase, true);
  assert.equal(find(s5, hasClass('ss-x-key-read')).length, 1, 'item 7 reading line');
  assert.equal(find(s5, hasClass('ss-x-key-read'))[0].textContent, EN['explain.side.how_to_read']);
  const s6 = SS.explain.renderScene(s[5], kase, true);
  const pool = find(s6, hasClass('ss-x-pool'));
  assert.equal(pool.length, 1);
  assert.ok(allText(pool[0]).indexOf(EN['explain.own.pool_why']) >= 0, 'item 5 sentence sits with the pooled drawing');
  // the reading line says highlighted rows are endorsed steps: on the shipped case every added row is endorsed
  const rows = JSON.parse(JSON.stringify(SS.explain.rows(kase)));
  assert.ok(rows.filter((r) => r.added).length > 0);
  assert.ok(rows.filter((r) => r.added).every((r) => r.reference), 'every highlighted row is on the endorsed map');
  for (const k of ['explain.scene.endorsed_note', 'explain.side.how_to_read', 'explain.own.pool_why']) {
    assert.equal(/official script|rather than|\u2014/.test(EN[k]), false, k);
  }
});

test('synthetic_band_on_case_scenes', () => {
  const { SS, kase } = load();
  const band = EN['band.synthetic'];
  assert.equal(band, 'SYNTHETIC: corpus and traps written by the authors; test of the instrument');
  const s = SS.explain.scenes(kase);
  assert.deepEqual(plain(s.map((x) => !!x.band)), [true, true, true, true, true, false]);
  s.forEach((x, i) => {
    const el = SS.explain.renderScene(x, kase, true);
    const bands = find(el, hasClass('ss-x-band'));
    assert.equal(bands.length, i < 5 ? 1 : 0, x.id);
    if (i < 5) assert.equal(bands[0].textContent, band);
  });
});

test('case_from_shipped_corpus', () => {
  const { SS, kase } = load();
  const s = SS.explain.scenes(kase);
  // scene 2 draws the shipped endorsed map; its one missing box is the shipped wished node
  const s2 = SS.explain.renderScene(s[1], kase, true);
  const missing = find(s2, hasClass('ss-x-missing'));
  assert.equal(missing.length, 1);
  assert.equal(allText(missing[0]).indexOf('Bylaw officer') >= 0, true);
  const refMissing = kase.reference.filter((r) => r.missing).map((r) => r.label);
  assert.deepEqual(refMissing, ['Bylaw officer']);
  assert.deepEqual(sorted(find(s2, hasClass('ss-x-box')).map((b) => b.dataset.label)), sorted(kase.reference.map((r) => r.label)));
  // scene 3 draws the AI's map from the case, each box beside its quote
  const s3 = SS.explain.renderScene(s[2], kase, true);
  assert.deepEqual(find(s3, hasClass('ss-x-box')).map((b) => b.dataset.label), kase.ai.map((a) => a.label));
  const quotes = find(s3, (n) => hasClass('ss-x-quote')(n) && !hasClass('ss-x-quote-removed')(n)).map((q) => q.textContent);
  assert.deepEqual(quotes, kase.ai.map((a) => kase.transcript[a.turn].text));
  // scene 4 draws the no-interview map, with no quote at all
  const s4 = SS.explain.renderScene(s[3], kase, true);
  assert.deepEqual(sorted(find(s4, hasClass('ss-x-box')).map((b) => b.dataset.label)), sorted(kase.no_interview.map((n) => n.label)));
  assert.equal(find(s4, hasClass('ss-x-quote')).length, 0);
  assert.ok(allText(s4).indexOf(kase.domain) >= 0 && allText(s4).indexOf(kase.role) >= 0);
});

test('side_by_side_marks_what_listening_added', () => {
  const { SS, kase } = load();
  const rows = JSON.parse(JSON.stringify(SS.explain.rows(kase)));
  const ni = new Set(kase.no_interview.map((n) => n.concept));
  const added = kase.ai.filter((a) => !ni.has(a.concept)).map((a) => a.concept).sort();
  assert.ok(added.length > 0);
  assert.deepEqual(rows.filter((r) => r.added).map((r) => r.concept).sort(), added);
  // every box of every reading is in exactly one row
  for (const lane of ['reference', 'ai', 'no_interview']) {
    const got = rows.filter((r) => r[lane]).map((r) => r[lane].label).sort();
    assert.deepEqual(got, kase[lane].map((b) => b.label).sort(), lane);
  }
  const el = SS.explain.renderScene(SS.explain.scenes(kase)[4], kase, true);
  assert.equal(find(el, hasClass('ss-x-added')).length, added.length);
  // a box named in the transcript carries the transcript mark; the others do not
  const marked = find(el, hasClass('ss-x-attested')).map((b) => b.dataset.label).sort();
  const want = [].concat(kase.reference, kase.ai, kase.no_interview).filter((b) => b.turn !== null).map((b) => b.label).sort();
  assert.deepEqual(marked, want);
});

test('no_number_in_the_animation', () => {
  const { SS, kase } = load();
  const page = SS.explain.renderAll(kase, { reduced: true });
  const t = allText(page);
  assert.ok(t.length > 500);
  assert.equal(/\d/.test(t), false, 'a digit in: ' + (t.match(/.{0,30}\d.{0,30}/) || [''])[0]);
  const live = SS.explain.renderAll(kase, { reduced: false });
  assert.equal(/\d/.test(allText(live)), false);
});

test('no_null_result_is_animated', () => {
  const { SS, kase } = load();
  const t = allText(SS.explain.renderAll(kase, { reduced: true })).toLowerCase();
  for (const bad of ['pull', 'leak', 'drawn as received', 'homogeni', 'placebo', 'remedy', 'flatten', 'ideal']) {
    assert.equal(t.indexOf(bad), -1, bad);
  }
});

test('gloss_once_where_the_no_interview_lane_is_named', () => {
  const { SS, kase } = load();
  for (const reduced of [true, false]) {
    const t = allText(SS.explain.renderAll(kase, { reduced: reduced }));
    assert.ok(t.indexOf(EN['lane.no_interview']) >= 0);
    assert.equal(t.split(EN['lane.no_interview_gloss']).length - 1, 1, 'gloss once, reduced=' + reduced);
  }
});

test('each_scene_links_to_try_it', () => {
  const { SS, kase } = load();
  SS.explain.scenes(kase).forEach((x) => {
    const links = find(SS.explain.renderScene(x, kase, true), (n) => n.tagName === 'A' && n.getAttribute('href') === TRY_HREF);
    assert.equal(links.length, 1, x.id);
    assert.ok(links[0].textContent.length > 0);
  });
});

test('reduced_motion_is_six_still_frames_in_a_column', () => {
  const { SS, kase } = load();
  const page = SS.explain.renderAll(kase, { reduced: true });
  const scenes = find(page, hasClass('ss-x-scene'));
  assert.equal(scenes.length, 6);
  assert.ok(scenes.every((s) => !s.hidden), 'all shown');
  assert.equal(find(page, hasClass('ss-x-controls')).length, 0, 'no player controls');
  assert.ok(find(page, hasClass('ss-x-pending')).length === 0, 'every frame is shown');
  assert.ok(hasClass('ss-x-still')(page));
});

test('player_shows_one_scene_and_steps_frames', () => {
  const { SS, kase } = load();
  const page = SS.explain.renderAll(kase, { reduced: false });
  const scenes = find(page, hasClass('ss-x-scene'));
  assert.equal(scenes.length, 6);
  assert.deepEqual(plain(scenes.map((s) => s.hidden)), [false, true, true, true, true, true]);
  assert.equal(find(page, hasClass('ss-x-controls')).length, 1);
  const p = SS.explain.player(page, SS.explain.scenes(kase), { autoplay: false });
  p.show(4, true);
  assert.deepEqual(plain(scenes.map((s) => s.hidden)), [true, true, true, true, false, true]);
  assert.equal(find(scenes[4], hasClass('ss-x-pending')).length, 0, 'a shown-complete scene has every frame');
  p.show(1, false);
  assert.ok(find(scenes[1], hasClass('ss-x-pending')).length > 0, 'a fresh scene starts with frames to come');
  const before = find(scenes[1], hasClass('ss-x-pending')).length;
  p.step();
  assert.equal(find(scenes[1], hasClass('ss-x-pending')).length, before - 1);
  p.stop();
});

test('scene_from_hash', () => {
  const { SS } = load();
  assert.equal(SS.explain.sceneFromHash('#scene=ai'), 2);
  assert.equal(SS.explain.sceneFromHash('#theme=dark&scene=own'), 5);
  assert.equal(SS.explain.sceneFromHash('#scene=nope'), -1);
  assert.equal(SS.explain.sceneFromHash(''), -1);
});

test('install_command_and_copy_strings', () => {
  assert.equal(EN['explain.install_command'], 'python3 -m second_scribe.app');
  for (const k of ['explain.copy', 'explain.copied', 'explain.copy_fallback']) assert.equal(typeof EN[k], 'string', k);
});

test('every_key_used_exists', () => {
  const src = fs.readFileSync(path.join(JS_DIR, 'site-explain.js'), 'utf8');
  const keys = new Set();
  const re = /'((?:explain|lane|band|common|app)\.[a-z0-9_.]+)'/g;
  let m;
  while ((m = re.exec(src))) keys.add(m[1]);
  assert.ok(keys.size > 10);
  assert.deepEqual([...keys].filter((k) => !(k in EN)), []);
  const { SS, kase, errors } = load();
  SS.explain.renderAll(kase, { reduced: true });
  SS.explain.renderAll(kase, { reduced: false });
  assert.deepEqual(errors, [], 'no unknown key at render');
});

test('every_frame_is_its_own_element_inside_its_scene', () => {
  const { SS, kase } = load();
  SS.explain.scenes(kase).forEach((x) => {
    const sec = SS.explain.renderScene(x, kase, false);
    const frames = sec._ssFrames;
    assert.equal(frames.length, x.frames.length, x.id);
    assert.equal(new Set(frames).size, frames.length, x.id + ': frames share an element');
    for (const f of frames) assert.ok(find(sec, (n) => n === f).length === 1, x.id + ': frame not in the scene');
  });
  // scene 5: grid, then the marks wrapper inside it, then the added wrapper inside that, holding the rows
  const s5 = SS.explain.renderScene(SS.explain.scenes(kase)[4], kase, true);
  const [grid, marks, added] = s5._ssFrames;
  assert.ok(find(grid, (n) => n === marks).length === 1 && find(marks, (n) => n === added).length === 1);
  assert.ok(find(added, hasClass('ss-x-grid')).length === 1);
});

test('legend_is_a_list_of_four', () => {
  const { SS, kase } = load();
  const page = SS.explain.renderAll(kase, { reduced: true });
  const lists = find(page, hasClass('ss-x-legend-list'));
  assert.equal(lists.length, 1);
  assert.equal(lists[0].children.length, 4);
  assert.equal(find(page, hasClass('ss-x-screen')).length, 1);
  assert.equal(find(find(page, hasClass('ss-x-screen'))[0], hasClass('ss-x-scene')).length, 6);
});

test('ai_scene_removes_the_mapping_talk_first', () => {
  // council 30 (MUST 1): the interviewer's mapping talk (turn 6, the planted mapping_talk_only trap g4) is shown struck
  // out and tagged before the AI's boxes; no AI box quotes it, and Side by side no longer highlights its row.
  const { SS, kase } = load();
  assert.deepEqual(plain(kase.mapping_talk), [6]);
  const s3 = SS.explain.scenes(kase)[2];
  assert.deepEqual(plain(s3.frames.slice(0, 2)), ['removed:6', 'row:' + kase.ai[0].id]);
  const el = SS.explain.renderScene(s3, kase, true);
  const removed = find(el, hasClass('ss-x-removed'));
  assert.equal(removed.length, 1);
  const struck = find(removed[0], hasClass('ss-x-quote-removed'));
  assert.equal(struck.length, 1);
  assert.equal(struck[0].tagName, 'S');
  assert.equal(struck[0].textContent, kase.transcript[6].text);
  assert.equal(find(removed[0], hasClass('ss-x-removed-tag'))[0].textContent, EN['explain.removed_tag']);
  assert.equal(EN['explain.removed_tag'], 'removed before the AI reads');
  assert.equal(find(removed[0], hasClass('ss-x-box')).length, 0, 'no box is drawn from it');
  assert.ok(kase.ai.every((a) => kase.transcript[a.turn].speaker === 'participant'), 'every AI box quotes the participant');
  assert.equal(allText(el).indexOf('front desk staff member'), -1);
  // Side by side: highlighted = AI has it, no-interview map lacks it; Licensing clerk is now on the endorsed map only
  const rows = plain(SS.explain.rows(kase));
  const label = (r) => (r.reference || r.ai || r.no_interview).label;
  assert.deepEqual(rows.filter((r) => r.added).map(label),
    ['Parking permit', 'Insurance certificate', 'Owners group chat', 'Bylaw officer']);
  assert.deepEqual(rows.filter((r) => r.reference && !r.ai && !r.no_interview).map(label),
    ['Licensing clerk', 'Water tank approval']);
  assert.deepEqual(rows.filter((r) => r.reference && r.ai && r.no_interview).map(label),
    ['Licensing guide', 'Startup loan', 'Zoning clearance', 'Business licence']);
  // the link says the recorded run may differ
  assert.equal(EN['explain.try_link'], 'See how the recorded AI run drew this case in Try it');
});
