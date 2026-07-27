const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  PERF_ROUND_SEC, PERF_THEMES, evalKeyframes, sampleAction,
  buildRoundOrder, pickActiveAction, applyWearingTransition, perfShouldRender,
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

test('buildRoundOrder offers put_on when bare and never both sides of a pair at once', () => {
  const theme = PERF_THEMES.matrix;
  const alwaysLow = () => 0.1; // coin flip (<0.5) always fires
  const order = buildRoundOrder(theme, {}, alwaysLow);
  assert.ok(order.includes('put_on_sunglasses'));
  assert.ok(!order.includes('take_off_sunglasses'), 'bare state must never schedule the exit action');
  assert.ok(order.includes('wave'));
});

test('buildRoundOrder offers take_off (never put_on again) once already worn', () => {
  const alwaysLow = () => 0.1;
  const order = buildRoundOrder(PERF_THEMES.matrix, { put_on_sunglasses: true }, alwaysLow);
  assert.ok(order.includes('take_off_sunglasses'));
  assert.ok(!order.includes('put_on_sunglasses'), 'already-worn state must never schedule another enter');
});

test('buildRoundOrder can skip a pair entirely for a round, leaving costume state untouched', () => {
  const alwaysHigh = () => 0.99; // coin flip (<0.5) never fires, no pair action either direction
  const bare = buildRoundOrder(PERF_THEMES.matrix, {}, alwaysHigh);
  const worn = buildRoundOrder(PERF_THEMES.matrix, { put_on_sunglasses: true }, alwaysHigh);
  assert.ok(!bare.includes('put_on_sunglasses') && !bare.includes('take_off_sunglasses'));
  assert.ok(!worn.includes('put_on_sunglasses') && !worn.includes('take_off_sunglasses'));
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

test('applyWearingTransition sets worn on enter, clears on exit, ignores everything else', () => {
  const theme = PERF_THEMES.matrix;
  let wearing = {};
  wearing = applyWearingTransition(wearing, theme, 'put_on_sunglasses');
  assert.equal(wearing.put_on_sunglasses, true);
  wearing = applyWearingTransition(wearing, theme, 'wave');
  assert.equal(wearing.put_on_sunglasses, true, 'an unrelated oneshot must not touch costume state');
  wearing = applyWearingTransition(wearing, theme, 'take_off_sunglasses');
  assert.equal(wearing.put_on_sunglasses, false);
  assert.deepEqual(applyWearingTransition(wearing, theme, null), wearing);
});

test('costume persists across a round boundary when the round never scheduled a removal', () => {
  const theme = PERF_THEMES.matrix;
  // Round 1: put_on_sunglasses plays, no take_off scheduled this round (simulates either
  // the coin flip skipping it, or the round being cut short by a mood interruption).
  let wearing = applyWearingTransition({}, theme, 'put_on_sunglasses');
  assert.equal(wearing.put_on_sunglasses, true);
  // Round 2 starts: buildRoundOrder must treat the costume as still worn, never re-offer
  // put_on_sunglasses, and only roll for take_off_sunglasses.
  const order = buildRoundOrder(theme, wearing, () => 0.99);
  assert.ok(!order.includes('put_on_sunglasses'));
  // wearing itself (the actual visual truth) is untouched until an exit action actually plays.
  assert.equal(wearing.put_on_sunglasses, true);
});

test('perfShouldRender only allows the performance during idle mood', () => {
  assert.equal(perfShouldRender('idle'), true);
  for (const mood of ['pending', 'escalate', 'speak', 'wake', 'think', 'sleep', 'working']){
    assert.equal(perfShouldRender(mood), false, `${mood} must suppress the overlay`);
  }
});
