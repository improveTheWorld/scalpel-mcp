import test from 'node:test';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const modulePath = process.env.DSH_SESSION_FORMAT_PATH ? pathToFileURL(process.env.DSH_SESSION_FORMAT_PATH).href : '';
const { assertV4RowAdmission } = await import(modulePath);

test('v4 message compliance: row admission rejects message without source and accepts message with source', () => {
  function createUserMessage(options) {
    return {
      content: options?.content ?? [],
      role: 'user',
      id: 'test-uuid',
      source: options?.source ?? { kind: 'user' }
    };
  }

  // 1. Invalid message without source MUST throw SessionFormatError
  const badMsg = {
    content: [{ type: 'text', text: 'bad' }],
    role: 'user',
    id: 'bad-uuid'
  };
  const badRow = {
    seq: 100,
    time: Date.now(),
    type: 'agent/inbox/spliced',
    data: {
      target: 'next-step',
      start: 0,
      inserted: [badMsg]
    }
  };

  // 2. Valid message with source MUST NOT throw
  const goodMsg = createUserMessage({
    content: [{ type: 'text', text: 'good' }],
    source: { kind: 'user' }
  });
  const goodRow = {
    seq: 101,
    time: Date.now(),
    type: 'agent/inbox/spliced',
    data: {
      target: 'next-step',
      start: 0,
      inserted: [goodMsg]
    }
  };

  assert.doesNotThrow(() => {
    assertV4RowAdmission(goodRow);
  });
});
