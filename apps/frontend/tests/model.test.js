import test from 'node:test';
import assert from 'node:assert/strict';
import { delayLabel, freshness, escapeHtml, parseFrames, project, unproject } from '../src/model.js';
test('unavailable prediction is never displayed as zero delay', () => {
  assert.equal(delayLabel({status:'error',delay_s:null}), 'Недоступен');
  assert.equal(delayLabel({status:'ok',delay_s:0}), '0 мин');
  assert.equal(delayLabel({status:'ok',delay_s:120}), '+2 мин');
  assert.equal(delayLabel({status:'ok',delay_s:-90}), '−1,5 мин');
});
test('freshness rejects missing, old and future timestamps', () => {
  const now=Date.parse('2026-09-27T12:00:00Z');
  assert.equal(freshness({event_time:'2026-09-27T11:59:30Z'},now),true);
  assert.equal(freshness({event_time:'2026-09-27T11:50:00Z'},now),false);
  assert.equal(freshness({event_time:'2026-09-27T12:02:00Z'},now),false);
  assert.equal(freshness({},now),false);
});
test('SSE parser preserves chunk remainder and recognizes resync', () => {
  const a=parseFrames('id: 4\nevent: vehicle.updated\ndata: {"id":4}\n\n: heartbeat\n\nevent: res');
  assert.deepEqual(a.events,[{id:'4',type:'vehicle.updated',data:{id:4}}]);
  const b=parseFrames(a.rest+'ync_required\ndata: {}\n\n');
  assert.equal(b.events[0].type,'resync_required');
  assert.equal(b.rest,'');
});
test('API text cannot inject markup',()=>assert.equal(escapeHtml('<img onerror="x">'), '&lt;img onerror=&quot;x&quot;&gt;'));
test('geographic projection correctly locates prime meridian and equator',()=>{
  assert.deepEqual(project(0,0,1),[256,256]);
  const [lon,lat]=unproject(...project(37.62,55.75,11),11);
  assert.ok(Math.abs(lon-37.62)<1e-8 && Math.abs(lat-55.75)<1e-8);
});
