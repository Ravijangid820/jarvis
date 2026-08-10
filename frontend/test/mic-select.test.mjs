/**
 * mic-select — choosing between this device's microphones and the server's.
 *
 * The interesting behaviour is all in the failure paths: an unplugged device must not silently
 * become a different one, and an unreachable server microphone must not leave the user unable to
 * speak at all. Both are exercised here with fake `mediaDevices` / `localStorage`, so this runs
 * under plain `node --test` with no DOM.
 */
import assert from "node:assert/strict"
import test from "node:test"

import {
  MIC_KEY, MIC_KIND_KEY, MIC_SERVER_KEY,
  getMicSource, setMicSource, getPreferredMicId, labelsHidden, listMics, micLabel, openMicStream,
} from "../src/mic-select.js"

function fakeStorage(initial = {}) {
  const m = new Map(Object.entries(initial))
  return {
    getItem: k => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: k => m.delete(k),
    _map: m,
  }
}

/** A mediaDevices double. `present` is the set of deviceIds that can actually be opened. */
function fakeMedia({ devices = [], present = null, onGet = null } = {}) {
  return {
    enumerateDevices: async () => devices,
    getUserMedia: async (constraints) => {
      onGet?.(constraints)
      const exact = constraints?.audio?.deviceId?.exact
      if (exact && present && !present.includes(exact)) {
        const e = new Error("device gone"); e.name = "OverconstrainedError"; throw e
      }
      return { id: exact || "default", getTracks: () => [] }
    },
  }
}

test("listMics keeps only audio inputs and reports hidden labels", async () => {
  const mediaDevices = fakeMedia({
    devices: [
      { kind: "audioinput", deviceId: "default", label: "" },
      { kind: "audioinput", deviceId: "boya", label: "" },
      { kind: "videoinput", deviceId: "cam", label: "" },
      { kind: "audiooutput", deviceId: "spk", label: "" },
    ],
  })
  const mics = await listMics({ mediaDevices })
  assert.deepEqual(mics.map(m => m.deviceId), ["default", "boya"])
  assert.equal(labelsHidden(mics), true, "no labels yet → the picker should offer to unlock them")
  assert.equal(micLabel(mics[0], 0), "System default")
  assert.equal(micLabel(mics[1], 1), "Microphone 2")
})

test("labels are treated as visible as soon as any device is named", async () => {
  const mics = [{ deviceId: "a", label: "" }, { deviceId: "b", label: "Boya BY-M1" }]
  assert.equal(labelsHidden(mics), false)
  assert.equal(micLabel(mics[1], 1), "Boya BY-M1")
})

test("the chosen browser mic is requested exactly, not as a preference", async () => {
  const storage = fakeStorage({ [MIC_KEY]: "boya" })
  let seen = null
  const mediaDevices = fakeMedia({ present: ["boya"], onGet: c => { seen = c } })
  const { fellBack } = await openMicStream({ channelCount: 1 }, { storage, mediaDevices })
  assert.equal(fellBack, false)
  // `exact`, so an absent device fails loudly instead of the browser quietly substituting another.
  assert.deepEqual(seen.audio.deviceId, { exact: "boya" })
  assert.equal(seen.audio.channelCount, 1, "caller's own constraints survive")
})

test("an unplugged mic falls back to the default and forgets itself", async () => {
  const storage = fakeStorage({ [MIC_KEY]: "boya" })
  const mediaDevices = fakeMedia({ present: ["builtin"] })       // boya is gone
  const { fellBack } = await openMicStream({}, { storage, mediaDevices })
  assert.equal(fellBack, true, "caller must be able to tell the user which mic they actually got")
  assert.equal(getPreferredMicId({ storage }), "",
    "the stale choice is cleared so it can't resurface on a device that reuses the id")
})

test("a refused permission is not mistaken for a missing device", async () => {
  const storage = fakeStorage({ [MIC_KEY]: "boya" })
  const mediaDevices = {
    enumerateDevices: async () => [],
    getUserMedia: async () => { const e = new Error("denied"); e.name = "NotAllowedError"; throw e },
  }
  await assert.rejects(() => openMicStream({}, { storage, mediaDevices }), /denied/)
  assert.equal(getPreferredMicId({ storage }), "boya",
    "saying no once must not discard the user's saved choice")
})

test("source selection round-trips and keeps both kinds independently", () => {
  const storage = fakeStorage()
  setMicSource("browser", "boya", { storage })
  assert.deepEqual(getMicSource({ storage }), { kind: "browser", deviceId: "boya", serverDevice: "" })

  setMicSource("server", "plughw:1,0", { storage })
  assert.deepEqual(getMicSource({ storage }),
    { kind: "server", deviceId: "boya", serverDevice: "plughw:1,0" })

  // Switching back must not have lost the browser device chosen earlier.
  setMicSource("browser", "boya", { storage })
  assert.equal(getMicSource({ storage }).kind, "browser")
  assert.equal(storage.getItem(MIC_SERVER_KEY), "plughw:1,0")
})

test("a server choice with no device recorded degrades to the browser", () => {
  const storage = fakeStorage({ [MIC_KIND_KEY]: "server" })   // no MIC_SERVER_KEY
  assert.equal(getMicSource({ storage }).kind, "browser",
    "an unusable selection must not reach capture time")
})

test("storage being unavailable never breaks capture", async () => {
  const boom = { getItem() { throw new Error("blocked") }, setItem() { throw new Error("blocked") },
                 removeItem() { throw new Error("blocked") } }
  assert.equal(getPreferredMicId({ storage: boom }), "")
  assert.equal(getMicSource({ storage: boom }).kind, "browser")
  const { fellBack } = await openMicStream({}, { storage: boom, mediaDevices: fakeMedia() })
  assert.equal(fellBack, false)
})
