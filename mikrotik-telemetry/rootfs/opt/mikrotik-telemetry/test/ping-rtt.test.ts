import assert from "node:assert/strict";
import test from "node:test";
import { readReplySeconds } from "../src/ping.js";

test("parses BusyBox reply time without substituting command runtime for missing replies", () => {
  assert.equal(readReplySeconds("64 bytes from router: seq=0 ttl=64 time=0.212 ms"), 0.000212);
  assert.equal(readReplySeconds("1 packets transmitted, 0 packets received"), undefined);
});
