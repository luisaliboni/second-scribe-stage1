'use strict';
// Council 30 (MUST 2): the plain sentence on wished-for services, in the app's Results / Try it lead
// (SS.profile.lead, profile.js) and on The study (SS.study.findings, site-study.js).
// Row 5's primary cells are disjoint counts (metrics.leak_split, appendix Table 1 row 5): "flattened" = drawn as
// received while the AI's own quote says the service did happen; "counterfactual" = drawn as received although the
// AI's own quote says the service never came (the danger case, appendix Step 7). The sentence must report the danger
// case whenever it is above 0, and never say "none as received" when any wish was drawn as received.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { load, ROOT, JS_DIR } = require('./harness');

const EN = JSON.parse(fs.readFileSync(path.join(ROOT, 'second_scribe', 'app', 'strings', 'en.json'), 'utf8'));

function doc(ideals, flat, against) {
  return { rows: [{ row: 5, cells: [
    { name: 'n_ideals', x: 'primary', value: ideals },
    { name: 'flattened', x: 'primary', value: flat },
    { name: 'counterfactual', x: 'primary', value: against },
  ] }] };
}
function leadText(d, opts) {
  const env = load({ files: ['core.js', 'profile.js'] });
  const out = JSON.parse(JSON.stringify(env.SS.profile.lead(d, opts || {})));
  return out.filter((f) => f.id === 'flattened')[0].text;
}
function studyText(d) {
  const e = load({ files: ['core.js', 'strip.js', 'profile.js'] });
  vm.runInContext(fs.readFileSync(path.join(JS_DIR, 'site-study.js'), 'utf8'), e.window, { filename: 'site-study.js' });
  const f = JSON.parse(JSON.stringify(e.SS.study.findings(d))).filter((x) => x.id === 'flattened');
  return f.length ? f[0].text : null;
}

test('lead: both 0 says none was drawn as received', () => {
  assert.strictEqual(leadText(doc(7, 0, 0)),
    'Of the 7 services that participants marked as wished for, the AI drew none as received, so this run shows no flattened ideal.');
});

test('lead: flattened 0 with the danger case above 0 reports it and never says none', () => {
  const t = leadText(doc(7, 0, 3));
  assert.strictEqual(t, 'Of the 7 services that participants marked as wished for, the AI drew 3 as received. ' +
    "In each of these cases the AI's own quote said that the service never came.");
  assert.ok(!/drew none|no flattened ideal/.test(t), t);
});

test('lead: both above 0 counts both, and the danger case is not a part of the flattened ones', () => {
  assert.strictEqual(leadText(doc(7, 2, 3)), 'Of the 7 services that participants marked as wished for, the AI drew 5 as received. ' +
    "In 3 of these cases the AI's own quote said that the service never came. " +
    'The other 2, whose quotes say that the service did happen, are what the study calls flattened ideals.');
  assert.strictEqual(leadText(doc(7, 1, 3)).split('. ').pop(),
    'The other one, whose quote says that the service did happen, is what the study calls a flattened ideal.');
});

test('lead: flattened above 0 with no danger case keeps the flattened sentence', () => {
  assert.strictEqual(leadText(doc(7, 2, 0)), 'Of the 7 services that participants marked as wished for, the AI drew 2 as received, ' +
    "which the study calls a flattened ideal. In none of these cases did the AI's own quote say that the service never came.");
});

test('lead: mode B speaks of this researcher, in the same three cases', () => {
  const b = (f, a) => leadText(doc(7, f, a), { referenceType: 'B' });
  assert.ok(b(0, 0).indexOf('this researcher marked') >= 0);
  assert.ok(b(0, 3).indexOf('this researcher marked as wished for, the AI drew 3 as received.') >= 0, b(0, 3));
  assert.ok(b(2, 3).indexOf('drew 5 as received') >= 0 && b(2, 3).indexOf('In 3 of these cases') >= 0, b(2, 3));
});

test('lead: one marked service drawn as received against its own quote', () => {
  assert.strictEqual(leadText(doc(1, 0, 1)), 'Participants marked 1 service as wished for, and the AI drew it as received. ' +
    "In each of these cases the AI's own quote said that the service never came.");
});

test('study: the finding reports the danger case and never says no case when one was drawn as received', () => {
  const none = studyText(doc(90, 4, 0));
  assert.strictEqual(none, 'In 4 cases the participant marked a service as wished for and the AI drew it as received, which the study ' +
    'calls a flattened ideal. In no case did the AI draw a wished-for service as received although its own quote says the ' +
    'service never came, and a team checks for this kind of distortion before it uses AI readings.');
  const both = studyText(doc(90, 4, 2));
  assert.ok(both.indexOf('In 2 other cases the AI drew a wished-for service as received although its own quote says the service never came') >= 0, both);
  const danger = studyText(doc(90, 0, 2));
  assert.strictEqual(danger, 'In 2 cases the participant marked a service as wished for and the AI drew it as received although its ' +
    'own quote says the service never came, and a team checks for this kind of distortion before it uses AI readings.');
  assert.ok(!/In 0 cases|In no case/.test(danger), danger);
  const zero = studyText(doc(90, 0, 0));
  assert.strictEqual(zero, 'In no case did the AI draw as received a service that the participant marked as wished for, and a team ' +
    'checks for this kind of distortion before it uses AI readings.');
});

test('glossary: flattened follows appendix Step 7', () => {
  assert.strictEqual(EN['glossary.flattened'], 'A flattened ideal is a service the participant marked as wished for that the AI ' +
    "map drew as received, while the AI's own quote says that the service did happen.");
});
