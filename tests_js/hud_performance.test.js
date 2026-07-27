const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  PERF_ROUND_SEC, PERF_THEMES, evalKeyframes, sampleAction,
  buildRoundOrder, pickActiveAction, isPairHeld, perfShouldRender,
} = require('../hud_performance.js');

test('evalKeyframes interpolates linearly between keyframes', () => {
  const kf = [{t:0,v:0},{t:1,v:10}];
  assert.equal(evalKeyframes(kf, 0), 0);
  assert.equal(evalKeyframes(kf, 0.5), 5);
  assert.equal(evalKeyframes(kf, 1), 10);
});

test('evalKeyframes clamps outside the keyframe range', () => {
  const kf = [{t:0.2,v:1},{t:0.8,v:9}];
  assert.equal(evalKeyframes(kf, 0), 1);
  assert.equal(evalKeyframes(kf, 1), 9);
});

test('sampleAction applies emotion gain and gazeBias on top of the base curve', () => {
  const action = { dur: 1, channels: { scale: [{t:0,v:1},{t:1,v:2}] } };
  const neutral = sampleAction(action, { gain: {}, gazeBias: 0 }, 1);
  assert.equal(neutral.scale, 2);
  const confident = sampleAction(action, { gain: { scale: 1.5 }, gazeBias: 0.15 }, 1);
  assert.equal(confident.scale, 3);
  assert.equal(confident.gazeDir, 0.15);
});

test('buildRoundOrder keeps enter/exit pair order and shuffles oneshots between them', () => {
  const theme = PERF_THEMES.matrix;
  const rand = (() => { let i = 0; const seq = [0.9, 0.1, 0.5]; return () => seq[i++ % seq.length]; })();
  const order = buildRoundOrder(theme, rand);
  const enterIdx = order.indexOf('put_on_sunglasses');
  const exitIdx = order.indexOf('take_off_sunglasses');
  assert.ok(enterIdx < exitIdx, 'enter must come before its matching exit');
  assert.ok(order.includes('wave'));
  assert.equal(order.length, 3);
});

test('buildRoundOrder never reorders a pair across many random seeds', () => {
  for (let i = 0; i < 50; i++){
    const order = buildRoundOrder(PERF_THEMES.matrix, Math.random);
    assert.ok(order.indexOf('put_on_sunglasses') < order.indexOf('take_off_sunglasses'));
  }
});

test('pickActiveAction walks the order by cumulative duration', () => {
  const theme = { actions: {
    a: { dur: 1, channels: {} }, b: { dur: 2, channels: {} },
  } };
  const order = ['a', 'b'];
  assert.equal(pickActiveAction(theme, order, 0.5).id, 'a');
  assert.equal(pickActiveAction(theme, order, 1.5).id, 'b');
  assert.equal(pickActiveAction(theme, order, 1.5).localT, 0.5);
  assert.equal(pickActiveAction(theme, order, 10), null);
});

test('7-minute round boundary: pickActiveAction returns null once elapsed exceeds the round', () => {
  const theme = PERF_THEMES.matrix;
  const order = ['put_on_sunglasses', 'wave', 'take_off_sunglasses'];
  const totalDur = order.reduce((s, id) => s + (theme.actions[id].dur ?? 1.2), 0);
  assert.ok(totalDur < PERF_ROUND_SEC, 'a single round of actions must fit well inside the 7-minute replay window');
  assert.equal(pickActiveAction(theme, order, totalDur + 0.01), null);
});

test('isPairHeld is true from enter start until exit finishes', () => {
  const theme = PERF_THEMES.matrix;
  const order = ['put_on_sunglasses', 'wave', 'take_off_sunglasses'];
  assert.equal(isPairHeld(theme, order, 'put_on_sunglasses', -0.1), false);
  assert.equal(isPairHeld(theme, order, 'put_on_sunglasses', 0.1), true);
  assert.equal(isPairHeld(theme, order, 'put_on_sunglasses', 0.6 + 1.8 + 0.1), true);
  assert.equal(isPairHeld(theme, order, 'put_on_sunglasses', 0.6 + 1.8 + 0.6 + 0.1), false);
});

test('perfShouldRender only allows the performance during idle mood', () => {
  assert.equal(perfShouldRender('idle'), true);
  for (const mood of ['pending', 'escalate', 'speak', 'wake', 'think', 'sleep', 'working']){
    assert.equal(perfShouldRender(mood), false, `${mood} must suppress the overlay`);
  }
});
