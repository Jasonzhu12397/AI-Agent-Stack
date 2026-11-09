import test from "node:test";
import assert from "node:assert/strict";
import {
  getKnowledgeDocumentError,
  getKnowledgeDocumentStatus,
} from "../src/domain/knowledgeDocument.ts";

test("knowledge document statuses have readable labels and semantic tag types", () => {
  assert.deepEqual(getKnowledgeDocumentStatus("pending"), { label: "Queued", type: "info" });
  assert.deepEqual(getKnowledgeDocumentStatus("ingesting"), { label: "Indexing", type: "warning" });
  assert.deepEqual(getKnowledgeDocumentStatus("ready"), { label: "Ready", type: "success" });
  assert.deepEqual(getKnowledgeDocumentStatus("failed"), { label: "Failed", type: "danger" });
});

test("unknown knowledge document statuses fail safely", () => {
  assert.deepEqual(getKnowledgeDocumentStatus("unexpected"), { label: "Unknown", type: "info" });
});

test("knowledge document failure details prefer message and fall back to code", () => {
  assert.equal(getKnowledgeDocumentError({ message: "Embedding provider unavailable" }), "Embedding provider unavailable");
  assert.equal(getKnowledgeDocumentError({ code: "EMBEDDING_FAILED" }), "EMBEDDING_FAILED");
  assert.equal(getKnowledgeDocumentError({ message: " ", code: "INDEX_FAILED" }), "INDEX_FAILED");
  assert.equal(getKnowledgeDocumentError(null), "");
  assert.equal(getKnowledgeDocumentError({ retryable: true }), "");
});
