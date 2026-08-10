/**
 * mic-select.js — which microphone the user's voice input comes from.
 *
 * One list, two kinds of source, because "the best microphone" is a question about the room rather
 * than about which machine owns the hardware:
 *
 *   kind: "browser"  an input on this device — the laptop's built-in array, a headset, a USB mic
 *                    plugged in here. Captured with getUserMedia.
 *   kind: "server"   a microphone attached to the Jarvis box, streamed to this tab as PCM. If the
 *                    good mic is sitting next to the server and the user is sitting next to it too,
 *                    that beats the laptop lid.
 *
 * Either way the caller gets a MediaStream and the rest of the voice pipeline — VAD, wake word,
 * Whisper — is unchanged, and transcription still happens in this tab.
 *
 * Distinct from the admin's ⊕ → Server microphone panel: that chooses the device the always-on
 * wake-word *listener* opens on the box. This chooses where one user's own voice input comes from.
 *
 * The preference is per-browser (localStorage), not per-account: it names hardware reachable from
 * THIS machine, so syncing it through the server would push a laptop's headset onto a phone.
 *
 * Every function takes an optional `deps` so the logic is testable under plain `node --test` with
 * no DOM — see test/mic-select.test.mjs.
 */

import { openServerMicStream } from "./server-mic.js"

export const MIC_KEY = "jarvis_mic_device"
// Stored separately from the browser deviceId so switching back and forth doesn't lose either.
export const MIC_KIND_KEY = "jarvis_mic_kind"
export const MIC_SERVER_KEY = "jarvis_mic_server_device"

function store(deps) { return deps.storage || globalThis.localStorage }
function devices(deps) { return deps.mediaDevices || globalThis.navigator?.mediaDevices }

/** The chosen deviceId, or "" for "let the browser decide". */
export function getPreferredMicId(deps = {}) {
  try { return store(deps)?.getItem(MIC_KEY) || "" } catch { return "" }
}

export function setPreferredMicId(deviceId, deps = {}) {
  try {
    if (deviceId) store(deps)?.setItem(MIC_KEY, deviceId)
    else store(deps)?.removeItem(MIC_KEY)
  } catch { /* private mode / storage disabled — the choice just won't persist */ }
}

export function clearPreferredMic(deps = {}) { setPreferredMicId("", deps) }

/** The chosen source: {kind: "browser"|"server", deviceId, serverDevice}. */
export function getMicSource(deps = {}) {
  const s = store(deps)
  let kind = "browser", serverDevice = ""
  try {
    kind = s?.getItem(MIC_KIND_KEY) === "server" ? "server" : "browser"
    serverDevice = s?.getItem(MIC_SERVER_KEY) || ""
  } catch { /* storage disabled — fall back to the browser default */ }
  // A "server" choice with no device recorded is not usable; treat it as unset rather than
  // failing later at capture time.
  if (kind === "server" && !serverDevice) kind = "browser"
  return { kind, deviceId: getPreferredMicId(deps), serverDevice }
}

/** Record the chosen source. `deviceId` is a browser deviceId, or the ALSA id for a server mic. */
export function setMicSource(kind, deviceId, deps = {}) {
  const s = store(deps)
  try {
    if (kind === "server") {
      s?.setItem(MIC_KIND_KEY, "server")
      s?.setItem(MIC_SERVER_KEY, deviceId || "")
    } else {
      s?.setItem(MIC_KIND_KEY, "browser")
      setPreferredMicId(deviceId, deps)
    }
  } catch { /* the choice just won't persist */ }
}

/**
 * The audio inputs this browser can see: [{ deviceId, label, isDefault }].
 *
 * `label` is deliberately allowed to come back empty. Browsers hide device labels until the page
 * has been granted microphone access at least once — reporting that honestly lets the picker offer
 * a one-click unlock instead of a permission prompt the user did not ask for.
 */
export async function listMics(deps = {}) {
  const md = devices(deps)
  if (!md?.enumerateDevices) return []
  const all = await md.enumerateDevices()
  return all
    .filter(d => d.kind === "audioinput")
    .map(d => ({ deviceId: d.deviceId, label: d.label || "", isDefault: d.deviceId === "default" }))
}

/** True when devices exist but the browser is withholding their names pending permission. */
export function labelsHidden(mics) {
  return mics.length > 0 && mics.every(m => !m.label)
}

/**
 * Open and immediately release a stream, purely to make the browser reveal device labels.
 * Resolves false if permission was refused — the picker still works, just with generic names.
 */
export async function unlockMicLabels(deps = {}) {
  const md = devices(deps)
  if (!md?.getUserMedia) return false
  try {
    const s = await md.getUserMedia({ audio: true })
    s.getTracks().forEach(t => t.stop())
    return true
  } catch { return false }
}

/** A device that is simply not here any more, as opposed to one we were refused. */
function isMissingDevice(err) {
  const name = err?.name || ""
  return name === "OverconstrainedError" || name === "NotFoundError" || name === "DevicesNotFoundError"
}

/**
 * getUserMedia with the user's chosen microphone, degrading sensibly when it has been unplugged.
 *
 * `exact` rather than `ideal` on purpose: `ideal` silently substitutes another device, which is the
 * one outcome worth avoiding — someone who picked their headset should not end up recorded through
 * the laptop lid without being told. So we ask for exactly that device, and if it is genuinely gone
 * we forget the preference, fall back to the default, and report `fellBack` so the caller can say
 * so out loud.
 *
 * A permission refusal is NOT a missing device: it propagates untouched and leaves the preference
 * alone, so saying "no" once doesn't quietly discard the setting.
 *
 * @returns {{stream: MediaStream, fellBack: boolean}}
 */
export async function openMicStream(audio = {}, deps = {}) {
  const md = devices(deps)
  if (!md?.getUserMedia) throw new Error("This browser cannot access microphones")
  const wanted = getPreferredMicId(deps)
  if (!wanted) return { stream: await md.getUserMedia({ audio }), fellBack: false }
  try {
    return { stream: await md.getUserMedia({ audio: { ...audio, deviceId: { exact: wanted } } }),
             fellBack: false }
  } catch (err) {
    if (!isMissingDevice(err)) throw err
    clearPreferredMic(deps)
    return { stream: await md.getUserMedia({ audio }), fellBack: true }
  }
}

/** A display name for a device the browser hasn't named yet. */
export function micLabel(mic, index) {
  return mic.label || (mic.isDefault ? "System default" : `Microphone ${index + 1}`)
}

/**
 * Open whichever microphone the user chose — on this device or on the server — as a MediaStream.
 *
 * The single entry point every voice feature should call. Returns `stop()` because a server stream
 * needs its fetch aborted and its audio graph torn down; for a browser stream it just stops the
 * tracks, so callers can treat both the same and not leak a held-open server microphone.
 *
 * `fellBack` is true when the chosen source was unavailable and the browser default was used
 * instead, so the UI can say so rather than let someone wonder why they sound distant.
 *
 * @returns {{stream: MediaStream, stop: () => void, fellBack: boolean, note: string}}
 */
export async function openSelectedMic(audio = {}, ctx = {}, deps = {}) {
  const { kind, serverDevice } = getMicSource(deps)
  if (kind === "server" && ctx.apiBase && ctx.token) {
    try {
      const { stream, stop } = await openServerMicStream(ctx.apiBase, ctx.token, serverDevice,
        { onEnd: ctx.onServerEnd })
      return { stream, stop, fellBack: false, note: "" }
    } catch (e) {
      // Busy, unplugged, or /dev/snd missing from the container. None of these should leave the
      // user unable to talk, so drop to their own microphone and tell them which one they got.
      // fellBack is unconditionally true here regardless of what the browser path reports: the
      // user asked for the server's mic and did not get it, which is the fact worth surfacing.
      const { stream } = await openMicStream(audio, deps)
      return {
        stream, fellBack: true,
        stop: () => stream.getTracks().forEach(t => t.stop()),
        note: `Server microphone unavailable (${e?.message || e}) — using this device's mic instead.`,
      }
    }
  }
  const { stream, fellBack } = await openMicStream(audio, deps)
  return {
    stream, fellBack,
    stop: () => stream.getTracks().forEach(t => t.stop()),
    note: fellBack ? "Your chosen microphone isn't connected — using the default one." : "",
  }
}
