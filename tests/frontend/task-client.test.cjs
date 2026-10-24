const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");

function browser() {
  const memory = new Map(),
    elements = new Map();
  const context = vm.createContext({
    sessionStorage: {
      getItem: (key) => memory.get(key),
      setItem: (key, value) => memory.set(key, value),
      removeItem: (key) => memory.delete(key),
    },
    document: {
      querySelector: (key) => {
        if (!elements.has(key)) elements.set(key, {});
        return elements.get(key);
      },
    },
    crypto: webcrypto,
    AbortSignal,
    setInterval,
    clearInterval,
    Date,
    activeTrip: { id: "old", createdAt: 1, updatedAt: 2, plan: {} },
    progressSource: null,
    disk: [],
    persisted: false,
    parseSsePayload: JSON.parse,
    setProgress() {},
    completeProgress() {},
    failProgress() {},
    showToast() {},
  });
  context.readSavedTrips = () => context.disk;
  context.saveTrip = (trip) => {
    context.persisted = structuredClone(trip);
    return !context.quota;
  };
  vm.runInContext(fs.readFileSync("static/task-client.js", "utf8"), context);
  return context;
}

test("lost POST response retries with exactly the same idempotency key and body", async () => {
  const b = browser(),
    calls = [];
  b.fetch = async (url, options) => {
    assert.ok(
      b.readPendingTask(),
      "recovery reference must exist before network starts",
    );
    calls.push(options);
    if (calls.length === 1) throw new TypeError("network lost");
    return { ok: true, json: async () => ({ task_id: "server-id" }) };
  };
  const result = await b.postBackgroundTask("plan", { city: "揭阳" });
  assert.equal(result.task_id, "server-id");
  assert.equal(
    calls[0].headers["Idempotency-Key"],
    calls[1].headers["Idempotency-Key"],
  );
  assert.equal(calls[0].body, calls[1].body);
  assert.equal(b.readPendingTask().taskId, "server-id");
});

test("uncertain submission keeps its recovery reference and blocks another submission", async () => {
  const b = browser();
  let calls = 0;
  b.fetch = async () => {
    calls++;
    throw new TypeError("offline");
  };
  await assert.rejects(
    b.postBackgroundTask("plan", {}),
    (error) => error.recoverable,
  );
  const pending = b.readPendingTask();
  assert.ok(pending.key);
  await assert.rejects(
    b.postBackgroundTask("plan", {}),
    (error) => error.recoverable,
  );
  assert.equal(calls, 2);
  assert.equal(b.readPendingTask().key, pending.key);
});

test("explicit queue rejection clears a submission that was not accepted", async () => {
  const b = browser();
  b.fetch = async () => ({
    ok: false,
    json: async () => ({ detail: "队列已满" }),
  });
  await assert.rejects(
    b.postBackgroundTask("plan", {}),
    (error) => error.detail === "队列已满",
  );
  assert.equal(b.readPendingTask(), null);
});

test("gateway failure keeps submission identity because execution may already have started", async () => {
  const b = browser();
  const keys = [];
  b.fetch = async (url, options) => {
    keys.push(options.headers["Idempotency-Key"]);
    return { ok: false, status: 502, json: async () => ({}) };
  };
  await assert.rejects(
    b.postBackgroundTask("plan", {}),
    (error) => error.recoverable,
  );
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);
  assert.equal(b.readPendingTask().key, keys[0]);
});

test("a revision result becomes a copy if another tab updated the original while waiting", () => {
  const b = browser();
  b.disk = [{ id: "old", updatedAt: 3 }];
  b.writePendingTask({
    key: "key",
    kind: "revision",
    taskId: "task",
    tripId: "old",
    baseVersion: 2,
    outputId: "copy",
  });
  b.saveBackgroundResult("task");
  assert.equal(b.persisted.id, "copy");
  assert.equal(b.disk[0].updatedAt, 3);
  assert.equal(b.readPendingTask(), null);
});

test("storage failure retains completed task for later recovery", () => {
  const b = browser();
  b.quota = true;
  b.writePendingTask({ key: "key", kind: "plan", taskId: "task" });
  assert.equal(b.saveBackgroundResult("task"), false);
  assert.equal(b.readPendingTask().taskId, "task");
});

test("SSE failure reconciles completed result through snapshot without clearing before save", async () => {
  const b = browser();
  let stream;
  b.EventSource = class {
    constructor() {
      stream = this;
    }
    addEventListener() {}
    close() {
      this.closed = true;
    }
  };
  b.fetch = async () => ({
    ok: true,
    json: async () => ({
      status: "completed",
      result: { plan: { city: "揭阳" } },
    }),
  });
  b.writePendingTask({ key: "key", kind: "plan", taskId: "task" });
  const waiting = b.watchBackgroundTask("task");
  stream.onerror();
  assert.equal((await waiting).city, "揭阳");
  assert.equal(stream.closed, true);
  assert.equal(b.readPendingTask().taskId, "task");
});
