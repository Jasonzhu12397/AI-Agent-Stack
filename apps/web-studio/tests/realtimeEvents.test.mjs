import { test } from "node:test";
import assert from "node:assert/strict";
import { drainSseFrames, parseSseFrame } from "../src/domain/realtimeEvents.ts";

test("task frames keep their id and parsed JSON data", () => {
  const frame = parseSseFrame('id: 2026-11-26T10:00:00\nevent: task\ndata: {"id":"t1"}');
  assert.equal(frame.id, "2026-11-26T10:00:00");
  assert.equal(frame.event, "task");
  assert.equal(frame.data, '{"id":"t1"}');
});

test("keep-alive comments are not events", () => {
  assert.equal(parseSseFrame(": keep-alive"), null);
  assert.deepEqual(parseSseFrame("event: ready\ndata: {}"), {
    id: "",
    event: "ready",
    data: "{}",
  });
});

test("a partial trailing frame stays in the buffer for the next chunk", () => {
  const first = drainSseFrames('event: task\ndata: {"id":"a"}\n\nevent: task\ndata: {"id"');
  assert.equal(first.frames.length, 1);
  assert.equal(first.frames[0].data, '{"id":"a"}');
  assert.equal(first.frames[0].id, "");
  assert.equal(first.rest, 'event: task\ndata: {"id"');

  const second = drainSseFrames(first.rest + ':"b"}\n\n');
  assert.equal(second.frames.length, 1);
  assert.equal(second.frames[0].data, '{"id":"b"}');
  assert.equal(second.rest, "");
});

test("multi-line data and CRLF framing are reassembled", () => {
  const parsed = drainSseFrames("event: audit\r\ndata: {\"a\":1}\r\ndata: {\"b\":2}\r\n\r\n");
  assert.equal(parsed.frames.length, 1);
  assert.equal(parsed.frames[0].event, "audit");
  assert.equal(parsed.frames[0].data, '{"a":1}\n{"b":2}');
});
