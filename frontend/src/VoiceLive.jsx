/**
 * VoiceLive.jsx — hands-free voice conversation, running entirely in this tab.
 *
 * Why the browser and not the `voice/` daemon in the plan: the daemon assumed the microphone was
 * on the SERVER, and it isn't — the orchestrator runs in an unprivileged LXC with no /dev/snd, so
 * that path is blocked on host access and hardware that isn't wired up yet. The laptop running
 * this page already has a working mic, a Whisper build (WASM, same worker as push-to-talk), and
 * speakers. Putting the loop here needs no new hardware at all, and it makes the open tab the
 * listening indicator — which is the honest answer to "how do I know if it's listening".
 *
 * The loop: VAD segments an utterance -> Whisper transcribes just that clip -> /chat/stream
 * answers -> Piper speaks it sentence by sentence -> back to listening.
 *
 * Two modes:
 *   - Open mic: the page IS the gate — one click, then every utterance is a turn.
 *   - "Hey Jarvis": armed until it hears its name. Two detectors run side by side:
 *       * openWakeWord (~3 small ONNX models per 80 ms) fires INSTANTLY on "hey jarvis" — it is a
 *         classifier trained on that one phrase, and upstream ships no model for any other, so it
 *         cannot be extended by configuration.
 *       * short utterances are transcribed and matched against an editable phrase list, which is
 *         how "jarvis, are you there" and "wake up jarvis" work at all. Anything longer than a few
 *         seconds is discarded unheard — that is the room talking, not someone addressing Jarvis.
 *     So while armed the room IS transcribed, briefly and locally: audio never leaves the tab, and
 *     an utterance that does not name Jarvis is dropped without being stored or sent anywhere.
 *
 * One thing it deliberately does NOT do:
 *   - No full duplex. The recognizer is gated shut while Jarvis speaks. Even with the browser's
 *     echo canceller on, its own voice reaching the mic would be transcribed and sent back as the
 *     next turn — a conversation with itself. Half-duplex is the robust fix; barge-in can come later.
 */
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react"
import { RATE as WAKE_RATE } from "./wake-detect.js"
import { openSelectedMic } from "./mic-select.js"
import { createHud } from "./voice-hud.js"
import { DEFAULT_WAKE_PHRASES, isGreetingRemainder, matchWakePhrase, parsePhraseList } from "./wake-phrases.js"
import { ensureStt, isSttWarm, transcribeAudio } from "./stt-worker.js"
import {
  BLOCK, blockTiming, createUtteranceGate, END_SILENCE_MS, KEEP_TAIL_MS, MAX_UTTERANCE_MS,
  MIN_UTTERANCE_MS, PREROLL_MS, START_MS, WARMUP_MS,
} from "./vad.js"

const MicPicker = lazy(() => import("./MicPicker.jsx"))

// --- page-level tuning, in milliseconds ---------------------------------------------------------
// The segmentation thresholds themselves (BLOCK, PREROLL_MS, END_SILENCE_MS and the rest) live in
// vad.js next to the state machine that reads them, so the tests can drive it with the real
// numbers instead of a second copy of them. What is left here is this page's own behaviour.
//
// How long a pause has to last before Jarvis decides you have finished is adjustable from the page
// because the right value is personal: how long you pause mid-thought is not something a default
// can know. Persisted in localStorage.
const PAUSE_CHOICES = [1500, 3000, 5000, 8000]
// After a reply, keep listening this long without needing the wake word again — otherwise a
// back-and-forth means saying "hey Jarvis" before every single sentence.
const CONVERSATION_MS = 8000
// While ARMED the only thing worth hearing is a wake phrase, which is short and followed by a
// natural gap. Waiting out the conversational pause (up to 8s) before even looking would make
// "jarvis, are you there" feel broken, so the armed state segments on its own much tighter pause.
const WAKE_PAUSE_MS = 420
// A wake phrase is a few words. Anything longer is someone talking in the room, not addressing
// Jarvis — discard it without transcribing rather than spend Whisper on the conversation.
const WAKE_MAX_UTTERANCE_MS = 3500

const rms = (buf) => {
  let sum = 0
  for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
  return Math.sqrt(sum / buf.length)
}

export default function VoiceLive({ token, apiBase, onExit }) {
  // off | loading | listening | hearing | thinking | speaking | error
  const [phase, setPhase] = useState("off")
  const [turns, setTurns] = useState([])       // [{role:'you'|'jarvis', text}]
  const [partial, setPartial] = useState("")   // reply text as it streams
  const [error, setError] = useState("")
  const [modelStatus, setModelStatus] = useState("")
  const [micNote, setMicNote] = useState("")   // "we used a different mic than you picked, because…"
  const [pickerOpen, setPickerOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  // "open"  — every utterance is a turn (one click, then just talk)
  // "wake"  — armed: nothing is sent until "hey Jarvis", then it acknowledges and listens
  const [mode, setMode] = useState(() => localStorage.getItem("jarvis_voice_mode") || "open")
  const [wakeReady, setWakeReady] = useState(false)
  // Editable, because the trained spotter answers to exactly one phrase and this list is what
  // gives "jarvis, are you there" and "wake up jarvis" a way to work at all.
  const [wakePhrases, setWakePhrases] = useState(
    () => parsePhraseList(localStorage.getItem("jarvis_wake_phrases")))
  // How long a pause means "I've finished". Persisted, because it is a personal setting and
  // getting it wrong in either direction is the single most irritating thing this page can do.
  const [pauseMs, setPauseMs] = useState(() => {
    const v = Number(localStorage.getItem("jarvis_voice_pause_ms"))
    return PAUSE_CHOICES.includes(v) ? v : END_SILENCE_MS
  })

  const phaseRef = useRef("off")
  const gateRef = useRef(true)                 // true = ignore microphone entirely (half-duplex)
  const levelRef = useRef(0)                   // 0..1, drives the rings while listening
  const ttsLevelRef = useRef(0)                // 0..1, drives the rings while speaking

  const runTurnRef = useRef(null)      // transcribe() is declared before runTurn is
  const wakePhrasesRef = useRef(wakePhrases)   // read from the audio callback, which never re-binds
  const streamRef = useRef(null)
  const stopMicRef = useRef(null)      // source-specific teardown (server stream vs local tracks)
  const actxRef = useRef(null)
  const nodeRef = useRef(null)
  const speakerRef = useRef(null)
  const canvasRef = useRef(null)
  const rafRef = useRef(null)
  const blocksRef = useRef([])                 // utterance buffer of Float32Array blocks
  // The segmentation state machine — noise floor, warm-up, open/close counters. It lives in
  // vad.js so it can be driven by a level trace in a test instead of only by a real microphone.
  const vadRef = useRef(null)
  // Read from the audio callback, which is created once — plain state would be frozen at the
  // value it had when the mic opened, so changing the setting mid-session would do nothing.
  const pauseMsRef = useRef(pauseMs)
  const endBlocksRef = useRef(1)
  const msPerBlockRef = useRef(1000 * 2048 / 48000)
  const wakeWorkerRef = useRef(null)
  const modeRef = useRef(mode)
  const awakeUntilRef = useRef(0)   // 0 = armed; a future timestamp = currently in conversation
  const transcriptRef = useRef(null)
  const stickToBottomRef = useRef(true)
  const prevTurnCountRef = useRef(0)
  const scrollRafRef = useRef(0)

  const setPhaseBoth = useCallback((p) => { phaseRef.current = p; setPhase(p) }, [])

  /** Stop following if you scroll up to re-read something; resume once you return to the bottom. */
  const onTranscriptScroll = () => {
    const el = transcriptRef.current
    if (!el) return
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
  }

  // Follow the conversation. Same rules as the main chat view: a NEW turn re-pins to the bottom,
  // while the reply streaming in token by token only scrolls if you are still pinned — and those
  // updates are coalesced to one scroll per frame rather than one per token.
  useEffect(() => {
    const el = transcriptRef.current
    if (!el) return
    const newTurn = turns.length !== prevTurnCountRef.current
    prevTurnCountRef.current = turns.length
    if (newTurn) {
      stickToBottomRef.current = true
      el.scrollTop = el.scrollHeight
      return
    }
    if (!stickToBottomRef.current || scrollRafRef.current) return
    scrollRafRef.current = requestAnimationFrame(() => {
      scrollRafRef.current = 0
      el.scrollTo({ top: el.scrollHeight, behavior: partial ? "auto" : "smooth" })
    })
  }, [turns, partial])

  // Keep the audio callback's view of the threshold current, and remember the choice.
  useEffect(() => { modeRef.current = mode; localStorage.setItem("jarvis_voice_mode", mode) }, [mode])

  /** In wake mode, is the mic currently allowed to send what it hears? */
  const conversationOpen = () => modeRef.current === "open" || Date.now() < awakeUntilRef.current

  useEffect(() => {
    pauseMsRef.current = pauseMs
    endBlocksRef.current = Math.max(1, Math.round(pauseMs / msPerBlockRef.current))
    localStorage.setItem("jarvis_voice_pause_ms", String(pauseMs))
  }, [pauseMs])

  // ---- audio helpers -------------------------------------------------------------------------
  const resampleTo16k = useCallback(async (float32, sourceRate) => {
    const target = 16000
    const frames = Math.max(1, Math.round(float32.length * target / sourceRate))
    const off = new OfflineAudioContext(1, frames, target)
    const buf = off.createBuffer(1, float32.length, sourceRate)
    buf.copyToChannel(float32, 0)
    const src = off.createBufferSource()
    src.buffer = buf
    src.connect(off.destination)
    src.start()
    return (await off.startRendering()).getChannelData(0)
  }, [])

  /** Serialized Piper TTS: synthesize the next sentence while the previous one is still playing,
   *  and play strictly in order. `drain()` resolves when the queue empties, which is what keeps
   *  the microphone gated until the last syllable is out of the speakers. */
  const createSpeaker = useCallback(() => {
    let active = true
    let synthChain = Promise.resolve(null)
    let playChain = Promise.resolve()

    const synth = async (text) => {
      try {
        const res = await fetch(apiBase + "/tts", {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
          body: JSON.stringify({ text }),
        })
        if (!res.ok) return null
        return (await res.json()).audio || null
      } catch { return null }
    }

    const play = (b64) => new Promise(resolve => {
      if (!b64 || !active) return resolve()
      let el
      try { el = new Audio("data:audio/wav;base64," + b64) } catch { return resolve() }

      // Deliberately NOT routed through the AudioContext. Reading true output amplitude would mean
      // createMediaElementSource, which reroutes playback into the graph — and a
      // MediaElementAudioSourceNode whose media is judged CORS-cross-origin emits SILENCE rather
      // than failing loudly. Risking a mute assistant to make the rings track the waveform exactly
      // is a bad trade: you can hear that it is speaking, whereas you cannot hear that the mic is
      // live. So the listening amplitude stays real (that is the one that has to be) and the
      // speaking envelope is synthesised while the element plays straight to the speakers.
      let raf = null
      const start = performance.now()
      const pulse = () => {
        if (!active || el.ended || el.paused) { ttsLevelRef.current = 0; return }
        const t = (performance.now() - start) / 1000
        ttsLevelRef.current = 0.34 + 0.2 * Math.sin(t * 11) + 0.1 * Math.sin(t * 27)
        raf = requestAnimationFrame(pulse)
      }

      const done = () => {
        if (raf) cancelAnimationFrame(raf)
        ttsLevelRef.current = 0
        resolve()
      }
      el.onended = done
      el.onerror = done
      el.onplaying = pulse
      el.play().catch(done)
    })

    return {
      say(text) {
        const t = (text || "").trim()
        if (!t || !active) return
        const s = synthChain.then(() => (active ? synth(t) : null))
        synthChain = s.catch(() => null)
        playChain = playChain.then(async () => { if (active) await play(await s) }).catch(() => {})
      },
      drain() { return playChain },
      stop() { active = false; ttsLevelRef.current = 0 },
    }
  }, [apiBase, token])

  // ---- the turn: transcript -> answer -> speech ----------------------------------------------
  const runTurn = useCallback(async (text) => {
    setTurns(t => [...t, { role: "you", text }])
    setPhaseBoth("thinking")
    setPartial("")
    const speaker = speakerRef.current
    let answer = ""
    let spokenTo = 0

    // Speak each completed sentence as it arrives instead of waiting for the whole reply — at
    // roughly 6 tok/s, waiting for the last token would leave seconds of silence every turn.
    const flush = (final) => {
      if (!speaker) return
      const pending = answer.slice(spokenTo)
      if (final) {
        if (pending.trim()) { speaker.say(pending); spokenTo = answer.length }
        return
      }
      const m = pending.match(/^[\s\S]*[.!?\n](?=\s)/)
      if (m && m[0].trim()) { speaker.say(m[0]); spokenTo += m[0].length }
    }

    try {
      const res = await fetch(apiBase + "/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
        body: JSON.stringify({ text, session_id: "default", voice: true }),
      })
      if (!res.ok) throw new Error(res.status === 401 || res.status === 403
        ? "Session expired — sign in again on the main page." : `Server error (${res.status})`)
      const reader = res.body.getReader()
      const dec = new TextDecoder()
      let buf = ""
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const lines = buf.split("\n")
        buf = lines.pop() || ""
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue
          let evt
          try { evt = JSON.parse(line.slice(6)) } catch { continue }
          if (evt.content) {
            answer += evt.content
            setPartial(answer)
            if (phaseRef.current !== "speaking") setPhaseBoth("speaking")
            flush(false)
          }
          if (evt.error) throw new Error("The assistant backend failed.")
        }
      }
      flush(true)
    } catch (e) {
      const msg = String(e.message || e)
      setError(msg)
      if (speaker) speaker.say("Sorry — something went wrong.")
    }

    if (answer.trim()) setTurns(t => [...t, { role: "jarvis", text: answer.trim() }])
    setPartial("")
    if (speaker) await speaker.drain()      // stay deaf until the last syllable has played
    setError("")
    // A moment of grace so the tail of the speakers' output doesn't re-open the mic.
    setTimeout(() => {
      if (phaseRef.current === "off") return
      blocksRef.current = []
      vadRef.current?.reset()
      gateRef.current = false
      // Follow-ups don't need the wake word again for a while — otherwise every sentence of a
      // back-and-forth has to start with "hey Jarvis".
      if (modeRef.current === "wake") awakeUntilRef.current = Date.now() + CONVERSATION_MS
      setPhaseBoth(modeRef.current === "wake" ? "listening" : "listening")
    }, 250)
  }, [apiBase, token, setPhaseBoth])

  // Kept in a ref so transcribe(), declared above, can reach the latest runTurn without the two
  // useCallbacks having to be reordered around each other.
  useEffect(() => { runTurnRef.current = runTurn }, [runTurn])

  useEffect(() => {
    wakePhrasesRef.current = wakePhrases
    localStorage.setItem("jarvis_wake_phrases", wakePhrases.join(", "))
  }, [wakePhrases])

  const transcribe = useCallback(async (float32, sourceRate) => {
    gateRef.current = true
    setPhaseBoth("thinking")
    try {
      const pcm = await resampleTo16k(float32, sourceRate)
      const text = (await transcribeAudio(pcm)).trim()
      // Whisper emits bracketed markers like [BLANK_AUDIO] / (silence) for non-speech; treating
      // those as a user turn would ask the LLM to answer nothing, out loud, on a loop.
      const clean = text.replace(/[[(][^\])]*[\])]/g, "").trim()
      if (!clean || clean.length < 2) {
        gateRef.current = false
        setPhaseBoth("listening")
        return
      }
      runTurnRef.current?.(clean)
    } catch (e) {
      setError(String(e?.message || e) || "Could not process that audio.")
      gateRef.current = false
      setPhaseBoth("listening")
    }
  }, [resampleTo16k, setPhaseBoth])

  /** Woken by "hey Jarvis": acknowledge out loud, then listen for the actual command. */
  const onWake = useCallback(async () => {
    if (phaseRef.current === "off") return
    awakeUntilRef.current = Date.now() + CONVERSATION_MS
    gateRef.current = true                 // deaf while the acknowledgement plays
    setPhaseBoth("speaking")
    try {
      const res = await fetch(apiBase + "/greeting", { headers: { Authorization: "Bearer " + token } })
      if (res.ok) {
        const { text, audio } = await res.json()
        if (text) setTurns(t => [...t, { role: "jarvis", text }])
        if (audio) {
          await new Promise(resolve => {
            const el = new Audio("data:audio/wav;base64," + audio)
            el.onended = el.onerror = resolve
            el.play().catch(resolve)
          })
        }
      }
    } catch { /* no greeting is survivable; it still listens */ }
    // Reset the VAD so the acknowledgement's own tail can't be mistaken for the command.
    blocksRef.current = []
    vadRef.current?.reset()
    awakeUntilRef.current = Date.now() + CONVERSATION_MS
    gateRef.current = false
    setPhaseBoth("listening")
  }, [apiBase, token, setPhaseBoth])

  // ---- start / stop --------------------------------------------------------------------------
  const stop = useCallback(() => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current)
    rafRef.current = null
    speakerRef.current?.stop()
    speakerRef.current = null
    try { nodeRef.current?.disconnect() } catch { /* already torn down */ }
    nodeRef.current = null
    // stopMic also aborts the fetch and tears down the graph behind a SERVER microphone — stopping
    // the stream's tracks alone would leave the box's mic held open and the endpoint locked.
    stopMicRef.current?.()
    stopMicRef.current = null
    streamRef.current?.getTracks().forEach(t => t.stop())
    streamRef.current = null
    try { actxRef.current?.close() } catch { /* already closed */ }
    actxRef.current = null
    // Deliberately NOT terminated. The Whisper worker is shared and document-scoped (stt-worker.js),
    // and the wake worker is expensive to rebuild for no reason: throwing them away here is exactly
    // what made every "Stop listening" cost a full model load on the next "Go live".
    wakeWorkerRef.current?.postMessage({ type: "reset" })
    awakeUntilRef.current = 0
    blocksRef.current = []
    gateRef.current = true
    levelRef.current = 0
    ttsLevelRef.current = 0
    setPhaseBoth("off")
  }, [setPhaseBoth])


  /** An utterance heard while ARMED: transcribe it and decide whether it was addressed to Jarvis.
   *
   *  Anything that does not match a wake phrase is dropped without a trace — no turn, no history,
   *  nothing sent anywhere. That is the whole bargain of this mode: the room is listened to, but
   *  only the sentences that name Jarvis ever leave the VAD. */
  const listenForWakePhrase = useCallback(async (float32, sourceRate) => {
    try {
      const pcm = await resampleTo16k(float32, sourceRate)
      const heard = (await transcribeAudio(pcm)).trim()
      // Whisper labels non-speech with bracketed markers; they must not be matched against.
      const clean = heard.replace(/[[(][^\])]*[\])]/g, "").trim()
      if (!clean) return
      const { matched, remainder } = matchWakePhrase(clean, wakePhrasesRef.current)
      if (!matched) return                       // not for us — stay armed, say nothing
      if (isGreetingRemainder(remainder)) {
        onWake()                                 // acknowledge aloud, then listen for the command
        return
      }
      // The command arrived in the same breath ("Jarvis, what's the weather") — no reason to make
      // them say it twice. Open the conversation window and answer it directly.
      awakeUntilRef.current = Date.now() + CONVERSATION_MS
      gateRef.current = true
      setPhaseBoth("thinking")
      runTurnRef.current?.(remainder)
    } catch { /* a failed transcription while armed is not worth surfacing; stay armed */ }
  }, [resampleTo16k, onWake, setPhaseBoth])

  const start = useCallback(async () => {
    setError("")
    setPhaseBoth("loading")

    // Both workers are built at most ONCE per page. Rebuilding them on every "Go live" is what made
    // a one-off cost feel like a permanent tax.
    if (mode === "wake" && !wakeWorkerRef.current) {
      const ww = new Worker(new URL("./wake-worker.js", import.meta.url), { type: "module" })
      wakeWorkerRef.current = ww
      ww.onmessage = (e) => {
        const m = e.data || {}
        if (m.type === "ready") setWakeReady(true)
        else if (m.type === "wake") onWake()
        else if (m.type === "error") setError(m.error || "Wake word failed")
      }
      ww.postMessage({ type: "load" })
    } else if (mode === "wake") {
      wakeWorkerRef.current.postMessage({ type: "reset" })
      setWakeReady(true)                    // already loaded — no second warm-up to sit through
    }

    try {
      // Resolves on the spot when the model is already resident, so a warm page shows no loading
      // text at all. Only a genuinely cold start reports progress.
      if (!isSttWarm()) setModelStatus("Preparing the speech model…")
      await ensureStt((m) => {
        if (m.type === "status") {
          setModelStatus(m.phase === "preparing" ? "Preparing the model…" : "Downloading the speech model…")
        } else if (m.type === "progress" && m.total) {
          setModelStatus(`Downloading the speech model… ${Math.round((m.loaded / m.total) * 100)}%`)
        }
      })
      setModelStatus("")
      gateRef.current = false
      setPhaseBoth(modeRef.current === "wake" ? "armed" : "listening")
    } catch (e) {
      setError(String(e?.message || e))
      setPhaseBoth("error")
      return
    }

    try {
      // echoCancellation is not optional here: without it Jarvis's reply comes back through the
      // mic and becomes the next question. The gate below is the belt to this pair of braces.
      // (A server microphone gets no browser AEC — it's a different machine's audio — so for that
      // source the gate is the only thing standing between a reply and an echo loop.)
      const { stream, stop: stopMic, note } = await openSelectedMic(
        { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        { apiBase, token, onServerEnd: (why) => setMicNote(why) })
      setMicNote(note)
      streamRef.current = stream
      stopMicRef.current = stopMic
      // Ask for 16 kHz directly: it is what BOTH the wake detector and Whisper want, so the
      // browser's own resampler does the work once instead of us doing it twice in JS. If a
      // browser refuses the rate, downsampleTo16k below picks up the slack.
      let actx
      try { actx = new AudioContext({ sampleRate: WAKE_RATE }) } catch { actx = new AudioContext() }
      actxRef.current = actx
      if (actx.state === "suspended") await actx.resume()
      speakerRef.current = createSpeaker()

      const source = actx.createMediaStreamSource(stream)
      // ScriptProcessorNode is deprecated in favour of AudioWorklet, but it is universally
      // supported and keeps the whole VAD in one file. Swap it if a browser ever drops it.
      const node = actx.createScriptProcessor(BLOCK, 1, 1)
      // Convert the millisecond thresholds against the real sample rate, once, here — the audio
      // callback runs hundreds of times a second and should not be doing arithmetic on constants.
      const { msPerBlock, inBlocks } = blockTiming(actx.sampleRate, BLOCK)
      endBlocksRef.current = inBlocks(pauseMsRef.current)
      msPerBlockRef.current = msPerBlock
      const maxBlocks = inBlocks(MAX_UTTERANCE_MS)
      const wakeEndBlocks = inBlocks(WAKE_PAUSE_MS)
      const wakeMaxBlocks = inBlocks(WAKE_MAX_UTTERANCE_MS)
      vadRef.current = createUtteranceGate({
        prerollBlocks: inBlocks(PREROLL_MS),
        startBlocks: inBlocks(START_MS),
        warmupBlocks: inBlocks(WARMUP_MS),
        minBlocks: inBlocks(MIN_UTTERANCE_MS),
        keepTailBlocks: inBlocks(KEEP_TAIL_MS),
      })
      nodeRef.current = node

      const srcRate = actx.sampleRate
      // Identity at 16 kHz; otherwise average whole groups of samples — crude, but it low-passes
      // as it decimates, which naive sample-dropping does not.
      const downsampleTo16k = (block) => {
        if (srcRate === WAKE_RATE) return block
        const ratio = srcRate / WAKE_RATE
        const out = new Float32Array(Math.floor(block.length / ratio))
        for (let i = 0; i < out.length; i++) {
          const a = Math.floor(i * ratio), b = Math.min(block.length, Math.floor((i + 1) * ratio))
          let sum = 0
          for (let j = a; j < b; j++) sum += block[j]
          out[i] = b > a ? sum / (b - a) : 0
        }
        return out
      }

      node.onaudioprocess = (ev) => {
        const input = ev.inputBuffer.getChannelData(0)
        if (gateRef.current) { levelRef.current = 0; return }

        // Wake mode, armed: the ONLY thing done with this audio is keyword spotting. Nothing is
        // buffered, nothing is transcribed, nothing leaves the tab — which is the whole reason a
        // keyword spotter is worth having instead of running Whisper on the room.
        // ARMED: the trained spotter listens for "hey jarvis" and fires instantly, as before. In
        // parallel the VAD now captures short utterances so they can be transcribed and matched
        // against the phrase list — that is what makes "jarvis, are you there" and "wake up
        // jarvis" work, since openWakeWord has no trained model for either.
        const armed = modeRef.current === "wake" && !conversationOpen()
        if (armed) {
          const w = wakeWorkerRef.current
          if (w) w.postMessage({ type: "audio", pcm: downsampleTo16k(new Float32Array(input)) })
          if (phaseRef.current !== "armed") setPhaseBoth("armed")
        }

        const level = rms(input)
        levelRef.current = Math.min(1, level * 12)

        const vad = vadRef.current
        blocksRef.current.push(new Float32Array(input))
        // Until an utterance opens, hold only the preroll: enough that the first word survives the
        // detection delay, and no more.
        if (!vad.capturing && blocksRef.current.length > vad.prerollBlocks) blocksRef.current.shift()

        // The armed state segments on a much tighter pause and a much shorter cap: a wake phrase
        // is a few words followed by a natural gap, and waiting out the conversational pause
        // before even looking would make "jarvis, are you there" feel broken.
        const decision = vad.step(level, blocksRef.current.length, {
          endBlocks: armed ? wakeEndBlocks : endBlocksRef.current,
          maxBlocks: armed ? wakeMaxBlocks : maxBlocks,
        })
        if (decision.state === "opened" && !armed && phaseRef.current !== "hearing") setPhaseBoth("hearing")
        if (decision.state !== "closed") return

        // Utterance closed. Drop all but a short tail of the trailing pause — it is silence by
        // definition and only slows transcription down.
        const blocks = decision.dropTrailing > 0
          ? blocksRef.current.slice(0, -decision.dropTrailing)
          : blocksRef.current
        const tooLong = decision.tooLong
        blocksRef.current = []
        if (!decision.usable) return   // a click or a cough, not a sentence
        // Too long to be a wake phrase: the room is talking, not addressing Jarvis. Drop it
        // unheard rather than pay for a transcription that cannot match anything.
        if (armed && tooLong) return

        const total = blocks.reduce((n, b) => n + b.length, 0)
        const clip = new Float32Array(total)
        let off = 0
        for (const b of blocks) { clip.set(b, off); off += b.length }
        if (armed) listenForWakePhrase(clip, actx.sampleRate)
        else transcribe(clip, actx.sampleRate)
      }

      source.connect(node)
      // A ScriptProcessorNode only receives audio while connected to the graph's destination.
      // Zero gain so nothing is echoed back out of the speakers.
      const mute = actx.createGain()
      mute.gain.value = 0
      node.connect(mute)
      mute.connect(actx.destination)
    } catch (e) {
      setError(e?.name === "NotAllowedError"
        ? "Microphone permission was declined — live mode needs it."
        : `Could not open the microphone: ${e?.message || e}`)
      stop()
    }
  }, [createSpeaker, transcribe, listenForWakePhrase, setPhaseBoth, stop, mode, onWake, apiBase, token])

  useEffect(() => stop, [stop])   // release the mic the moment this page goes away

  // Escape closes whichever panel is on top. Bound once rather than per-panel so the two can't
  // disagree about which of them Escape belongs to.
  useEffect(() => {
    if (!settingsOpen && !pickerOpen) return
    const onKey = (e) => {
      if (e.key !== "Escape") return
      if (pickerOpen) setPickerOpen(false)
      else setSettingsOpen(false)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [settingsOpen, pickerOpen])

  // ---- the reactor face ----------------------------------------------------------------------
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    // Honour the app's "Reduce effects" toggle and the OS setting. Neither stops the face — the
    // amplitude readout is the point of this screen — they thin the particle spray and the glows.
    const reduce = localStorage.getItem("jarvis_perf") === "1" ||
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
    const hud = createHud(canvas, { reduce })

    const draw = () => {
      const p = phaseRef.current
      // The amplitude is REAL: microphone level while listening, Piper's output while speaking.
      // If this moved on a timer it would look identical with a dead mic, which is the one thing
      // this screen exists to rule out.
      const amp = p === "speaking" ? ttsLevelRef.current
        : (p === "listening" || p === "hearing" || p === "armed") ? levelRef.current : 0
      hud.draw({ phase: p, amp: Math.max(0, Math.min(1, amp)), now: performance.now() })
      rafRef.current = requestAnimationFrame(draw)
    }
    rafRef.current = requestAnimationFrame(draw)
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
      hud.dispose()
    }
  }, [])

  const LABEL = {
    off: "Tap to go live", loading: modelStatus || "Warming up…",
    armed: "Say “Hey Jarvis”", listening: "Listening",
    hearing: "Hearing you…", thinking: "Thinking", speaking: "Speaking", error: "Error",
  }

  return (
    <div className="voice-page">
      <header className="voice-head">
        <span className="voice-brand">J.A.R.V.I.S · LIVE</span>
        <div className="voice-head-actions">
          <button type="button" className="hud-btn voice-gear" onClick={() => setSettingsOpen(true)}
                  aria-label="Voice settings" title="Voice settings">⚙</button>
          <button type="button" className="hud-btn" onClick={() => { stop(); onExit?.() }}>Exit</button>
        </div>
      </header>

      <div className={`voice-stage phase-${phase}`}>
        {/* The canvas and its wordmark share a stacking context so the name sits inside the rings
            like the reference. Text stays real DOM text — crisper than canvas glyphs at any DPR,
            and it survives a screen reader, which a painted wordmark would not. */}
        <div className="voice-reactor">
          <canvas ref={canvasRef} className="voice-canvas" />
          <span className="voice-wordmark" aria-hidden="true">J.A.R.V.I.S.</span>
        </div>
        <div className="voice-label">{LABEL[phase] || phase}</div>
        {phase === "off" ? (
          <button type="button" className="login-btn voice-go" onClick={start}>Go live</button>
        ) : (
          <button type="button" className="hud-btn voice-go" onClick={stop}>Stop listening</button>
        )}
        {/* What used to be two rows of buttons. They are settings, not controls: you touch them
            once and then look at this screen for the next twenty minutes, so they live behind the
            gear and leave a five-word summary you can read at a glance instead. */}
        <button type="button" className="voice-summary" onClick={() => setSettingsOpen(true)}
                title="Voice settings">
          {mode === "wake" ? "“Hey Jarvis”" : "Open mic"} · {pauseMs / 1000}s pause
        </button>
        {mode === "wake" && phase !== "off" && (
          <span className="voice-wake-state">{wakeReady ? "wake word armed" : "loading wake word…"}</span>
        )}
        <p className="voice-hint">
          {phase === "off"
            ? "Your microphone stays in this tab — audio is transcribed here, never uploaded. Only the text is sent."
            : "Speak naturally, then pause. Jarvis is deaf while it talks, so wait for it to finish."}
        </p>
        {micNote && <div className="voice-hint voice-mic-note">{micNote}</div>}
        {error && <div className="voice-error">{error}</div>}
      </div>
      {settingsOpen && (
        <div className="jarvis-modal-backdrop" onClick={() => setSettingsOpen(false)}>
          <div className="jarvis-modal voice-settings" role="dialog" aria-modal="true"
               aria-label="Voice settings" onClick={e => e.stopPropagation()}>
            <div className="jm-header">
              <h3>Voice settings</h3>
              <button type="button" className="jm-close" onClick={() => setSettingsOpen(false)}>×</button>
            </div>
            <div className="jm-body">
              <div className="vs-row">
                <div className="vs-label">
                  <strong>How it listens</strong>
                  <span>Open mic treats every utterance as a turn. “Hey Jarvis” stays armed and
                    transcribes nothing until it hears the wake word.</span>
                </div>
                <div className="vs-choices">
                  <button type="button" className={`hud-btn${mode === "open" ? " active" : ""}`}
                    onClick={() => setMode("open")} disabled={phase !== "off"}>Open mic</button>
                  <button type="button" className={`hud-btn${mode === "wake" ? " active" : ""}`}
                    onClick={() => setMode("wake")} disabled={phase !== "off"}>“Hey Jarvis”</button>
                </div>
              </div>

              <div className="vs-row">
                <div className="vs-label">
                  <strong>Wake phrases</strong>
                  <span>What it answers to while armed, separated by commas. “Hey Jarvis” is
                    recognised instantly by a trained model; the rest are matched from the
                    transcript a moment later, which is why they can be anything you like.</span>
                </div>
                <div className="vs-choices vs-phrases">
                  <textarea className="hud-input" rows="3" defaultValue={wakePhrases.join(", ")}
                    onBlur={e => setWakePhrases(parsePhraseList(e.target.value))}
                    placeholder={DEFAULT_WAKE_PHRASES.join(", ")} />
                  <button type="button" className="hud-btn"
                    onClick={() => setWakePhrases([...DEFAULT_WAKE_PHRASES])}>Reset</button>
                </div>
              </div>

              <div className="vs-row">
                <div className="vs-label">
                  <strong>Wait for a pause of</strong>
                  <span>How long a silence means you've finished. Too short cuts you off
                    mid-thought; too long makes every reply feel slow.</span>
                </div>
                <div className="vs-choices">
                  {PAUSE_CHOICES.map(ms => (
                    <button key={ms} type="button"
                      className={`hud-btn${pauseMs === ms ? " active" : ""}`}
                      onClick={() => setPauseMs(ms)}>{ms / 1000}s</button>
                  ))}
                </div>
              </div>

              <div className="vs-row">
                <div className="vs-label">
                  <strong>Microphone</strong>
                  <span>Which input picks you up — this device's, or one attached to the server.</span>
                </div>
                <div className="vs-choices">
                  {/* Chosen when the stream opens, so changing it mid-session would promise a
                      switch that won't happen until you go live again. */}
                  <button type="button" className="hud-btn" disabled={phase !== "off"}
                    onClick={() => { setSettingsOpen(false); setPickerOpen(true) }}>Choose…</button>
                </div>
              </div>

              {phase !== "off" && (
                <p className="jm-desc">Stop listening to change how it listens or which microphone
                  it uses — those take effect when the stream opens.</p>
              )}
            </div>
          </div>
        </div>
      )}
      {pickerOpen && (
        <Suspense fallback={null}>
          <MicPicker token={token} apiBase={apiBase} onClose={() => setPickerOpen(false)}
                     onChange={() => setMicNote("")} />
        </Suspense>
      )}

      <div className="voice-transcript" ref={transcriptRef} onScroll={onTranscriptScroll}>
        {turns.length === 0 && !partial && <div className="voice-empty">Nothing said yet.</div>}
        {turns.map((t, i) => (
          <div key={i} className={`voice-turn ${t.role}`}>
            <span className="voice-who">{t.role === "you" ? "You" : "Jarvis"}</span>
            <span className="voice-text">{t.text}</span>
          </div>
        ))}
        {partial && (
          <div className="voice-turn jarvis">
            <span className="voice-who">Jarvis</span>
            <span className="voice-text">{partial}</span>
          </div>
        )}
      </div>
    </div>
  )
}
