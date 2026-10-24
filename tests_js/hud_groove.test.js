// node --test tests_js/hud_groove.test.js
const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  grooveNodBeats, grooveMode, groovePeriod, grooveRemainingSec, createGroove,
} = require('../hud_groove.js');

test('grooveNodBeats picks beats closest to 1 second, smaller on a tie', () => {
  assert.equal(grooveNodBeats(136), 2);
  assert.equal(grooveNodBeats(158), 2);
  assert.equal(grooveNodBeats(100), 2);
  assert.equal(grooveNodBeats(60), 1);
  assert.equal(grooveNodBeats(180), 2);
});

test('grooveMode: nod needs a finite bpm >= 120 and remainingSec >= 15', () => {
  assert.equal(grooveMode(119, Infinity), 'sway');
  assert.equal(grooveMode(120, Infinity), 'nod');
  assert.equal(grooveMode(null, Infinity), 'sway');
  assert.equal(grooveMode(130, 14.9), 'sway');
  assert.equal(grooveMode(130, 15), 'nod');
});

test('groovePeriod: sway without bpm is 6.5s, with bpm is 60*8/bpm; nod is 60*beats/bpm', () => {
  assert.equal(groovePeriod('sway', null), 6.5);
  assert.equal(groovePeriod('sway', 120), 4);
  assert.equal(groovePeriod('nod', 120), 1);
});

test('fades in while playing, idle, and a beat bpm is given', () => {
  const g = createGroove();
  let last = {};
  for (let t = 0; t <= 2; t += 0.02) {
    last = g.step(t, { playing: true, bpm: 120, remainingSec: Infinity, moodIdle: true });
  }
  const nonZero = (last.pitch || 0) !== 0 || (last.headTiltZ || 0) !== 0;
  assert.ok(nonZero, 'expected some nonzero groove amplitude after fading in');
});

test('yields (returns {}) when mood stops being idle', () => {
  const g = createGroove();
  let t = 0;
  for (; t <= 2; t += 0.02) {
    g.step(t, { playing: true, bpm: 120, remainingSec: Infinity, moodIdle: true });
  }
  let last = {};
  for (let i = 0; i < 100; i++) {
    t += 0.02;
    last = g.step(t, { playing: true, bpm: 120, remainingSec: Infinity, moodIdle: false });
  }
  assert.deepEqual(last, {});
});

test('yields (returns {}) when playback stops', () => {
  const g = createGroove();
  let t = 0;
  let last = {};
  for (; t <= 2; t += 0.02) {
    last = g.step(t, { playing: false, bpm: 120, remainingSec: Infinity, moodIdle: true });
  }
  assert.deepEqual(last, {});
});

test('nod alternates one heavy, one light beat (~0.16 and ~0.096 pitch peak)', () => {
  const g = createGroove();
  const bpm = 120; // period 1s
  let t = 0;
  for (; t < 5; t += 0.02) {
    g.step(t, { playing: true, bpm, remainingSec: Infinity, moodIdle: true });
  }
  const peaks = [];
  for (let cycle = 0; cycle < 2; cycle++) {
    let peak = 0;
    const end = t + 1;
    for (; t < end; t += 0.01) {
      const pv = g.step(t, { playing: true, bpm, remainingSec: Infinity, moodIdle: true });
      peak = Math.max(peak, pv.pitch || 0);
    }
    peaks.push(peak);
  }
  const hasHeavy = peaks.some(p => Math.abs(p - 0.16) <= 0.01);
  const hasLight = peaks.some(p => Math.abs(p - 0.096) <= 0.01);
  assert.ok(hasHeavy, `expected one peak near 0.16, got ${peaks}`);
  assert.ok(hasLight, `expected one peak near 0.096, got ${peaks}`);
});

test('sway without bpm reaches headTiltZ near 9 over a full period', () => {
  const g = createGroove();
  let t = 0;
  for (; t < 3; t += 0.02) {
    g.step(t, { playing: true, bpm: null, remainingSec: Infinity, moodIdle: true });
  }
  let maxTilt = 0;
  const end = t + 6.5;
  for (; t < end; t += 0.02) {
    const pv = g.step(t, { playing: true, bpm: null, remainingSec: Infinity, moodIdle: true });
    maxTilt = Math.max(maxTilt, Math.abs(pv.headTiltZ || 0));
  }
  assert.ok(Math.abs(maxTilt - 9) <= 0.5, `expected max headTiltZ near 9, got ${maxTilt}`);
});

test('outro fade: remainingSec=7.5 peaks about half of remainingSec=60', () => {
  // 歌尾 15 秒內一律搖擺，所以兩邊都用搖擺（bpm null）比歪頭幅度，才是同一種動作在比
  function peakAt(remainingSec) {
    const g = createGroove();
    let t = 0;
    for (; t < 3; t += 0.02) {
      g.step(t, { playing: true, bpm: null, remainingSec, moodIdle: true });
    }
    let peak = 0;
    const end = t + 7;  // 搖擺一輪 6.5 秒，量滿一輪
    for (; t < end; t += 0.01) {
      const pv = g.step(t, { playing: true, bpm: null, remainingSec, moodIdle: true });
      peak = Math.max(peak, Math.abs(pv.headTiltZ || 0));
    }
    return peak;
  }
  const near = peakAt(7.5);
  const far = peakAt(60);
  const ratio = near / far;
  assert.ok(Math.abs(ratio - 0.5) <= 0.05, `expected ratio ~0.5, got ${ratio} (near=${near}, far=${far})`);
});

test('grooveRemainingSec computes duration minus elapsed, Infinity when duration is 0', () => {
  assert.equal(grooveRemainingSec(240, 1000, 1100), 140);
  assert.equal(grooveRemainingSec(0, 1000, 1100), Infinity);
});

test('time going backwards or jumping does not produce a negative phase or throw', () => {
  const g = createGroove();
  let t = 0;
  for (; t < 2; t += 0.02) {
    g.step(t, { playing: true, bpm: 120, remainingSec: Infinity, moodIdle: true });
  }
  assert.doesNotThrow(() => {
    g.step(t - 5, { playing: true, bpm: 120, remainingSec: Infinity, moodIdle: true });
    g.step(t + 50, { playing: true, bpm: 120, remainingSec: Infinity, moodIdle: true });
  });
});
