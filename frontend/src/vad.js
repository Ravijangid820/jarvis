/**
 * Voice activity detection: the decisions, with no browser in them.
 *
 * Two pages listen to a microphone and have to answer the same two questions — "is this speech?"
 * and "have they stopped?" — from a stream of RMS levels. App.jsx does it for one push-to-talk
 * take; VoiceLive.jsx does it continuously, segmenting the room into utterances. Both used to
 * carry their own copy of the arithmetic inside an audio callback, where nothing could reach it:
 * the tests that covered this replayed a THIRD copy of the logic against synthetic traces and
 * never imported a line of the real thing. One of them had quietly drifted into testing an
 * algorithm the app had already stopped using — the frozen noise floor, whose replacement is
 * described below — and still passed.
 *
 * So the state machines live here, take levels as numbers, and return decisions. The callers keep
 * what genuinely needs a browser: AudioContext, buffers of samples, React refs.
 */

// Speech has to clear the noise floor by this much...
export const TRIGGER_OVER_NOISE = 3.0
// ...and this absolute floor, so a silent room cannot make the trigger vanishingly small.
export const MIN_RMS = 0.012
// Noise-floor tracking, asymmetric on purpose: fall quickly toward a quieter background, rise only
// slowly. A symmetric average lets a burst of speech drag the floor up with it.
export const NOISE_FALL = 0.30     // weight on a NEW, quieter observation
export const NOISE_RISE = 0.002    // weight when the room is louder than the floor (i.e. speech)

/**
 * An adaptive noise floor, and the speech trigger that rides on it.
 *
 * Seeded from the room on the first block rather than a hardcoded guess, and tracked on EVERY
 * block including mid-speech. Both of those are scar tissue: with a hardcoded seed and music
 * playing, the music itself read as speech within 85 ms; and freezing the floor once speech began
 * meant a background louder than the frozen trigger kept the level "loud" forever, so the pause
 * that should have ended the take never registered and it ran to the cap every time.
 */
export function createNoiseFloor({ fall = NOISE_FALL, rise = NOISE_RISE,
                                   minRms = MIN_RMS, overNoise = TRIGGER_OVER_NOISE } = {}) {
  let noise = -1
  let seen = 0
  return {
    /** Blocks observed so far — a warm-up gate needs to know the floor has seen the room. */
    get blocks() { return seen },
    get floor() { return noise < 0 ? 0 : noise },
    /** Judge one level against the floor, then fold it in. */
    update(level) {
      if (noise < 0) noise = level          // seed from reality, not a guess
      seen++
      const floor = noise
      noise = level < floor ? floor * (1 - fall) + level * fall
                            : floor * (1 - rise) + level * rise
      const trigger = Math.max(minRms, floor * overNoise)
      return { floor, trigger, loud: level > trigger }
    },
  }
}

// --- push-to-talk thresholds (App.jsx's mic button), in milliseconds --------------------------
// Silence long enough to mean "I've finished talking". Generous, because Whisper needs the
// trailing audio anyway and clipping someone mid-thought is far more annoying than waiting.
export const VOICE_SILENCE_MS = 1400
export const VOICE_NO_SPEECH_MS = 7000   // clicked the mic but never spoke -> give up quietly
// A memory backstop, not a limit on how long you may talk — the same reasoning as /voice's cap.
// Hitting it stops the take WITHOUT auto-sending, so a runaway recording lands in the box for
// review rather than firing a turn nobody finished.
export const VOICE_MAX_MS = 300000

/**
 * One push-to-talk take (App.jsx): ends on a pause after speech, or gives up.
 *
 * Only ever decides when to STOP the recorder — the recorder itself has captured every sample
 * meanwhile, so nothing here can clip the audio Whisper receives.
 *
 * `step` returns null while the take continues, or why it ended:
 *   { autoSubmit: true,  reason: "pause"   }  speech, then a real pause -> send it
 *   { autoSubmit: false, reason: "silence" }  clicked the mic, never spoke
 *   { autoSubmit: false, reason: "cap"     }  ran too long
 * Only "pause" fires a turn. The other two leave the transcript in the box, because a turn nobody
 * asked for is worse than one they have to click.
 */
export function createSilenceWatch({ silenceMs, noSpeechMs, maxMs, startedAt = 0,
                                     floor = createNoiseFloor() }) {
  let quietSince = 0
  let spoke = false
  return {
    get heardSpeech() { return spoke },
    step(level, now) {
      const { loud } = floor.update(level)
      if (loud) {
        spoke = true
        quietSince = 0
      } else if (!quietSince) {
        quietSince = now
      }
      const quietFor = quietSince ? now - quietSince : 0
      if (spoke && quietFor >= silenceMs) return { autoSubmit: true, reason: "pause" }
      if (!spoke && now - startedAt >= noSpeechMs) return { autoSubmit: false, reason: "silence" }
      if (now - startedAt >= maxMs) return { autoSubmit: false, reason: "cap" }
      return null
    },
  }
}

// --- continuous-segmentation tuning (VoiceLive.jsx), in milliseconds --------------------------
// Expressed as time, not block counts, and converted against the device's ACTUAL sample rate at
// start-up. A block is 2048 frames — 42.7 ms at 48 kHz but 46.4 ms at 44.1 kHz — so a hardcoded
// count silently means something different per machine, which is a poor way to express "wait about
// a second and a half".
export const BLOCK = 2048
export const PREROLL_MS = 430    // kept before speech is detected, so the first word survives
export const START_MS = 130      // sustained level above the trigger before an utterance opens
// How long a pause has to last before Jarvis decides you have finished. Generous on purpose:
// cutting someone off mid-thought is far more annoying than a moment of dead air, and people pause
// to think mid-sentence far more than they expect. The page lets this be changed (PAUSE_CHOICES);
// this is the default.
export const END_SILENCE_MS = 5000
export const MIN_UTTERANCE_MS = 350   // shorter than this is a cough or a click, not speech
// A memory backstop, NOT a limit on how long you may speak. Buffered audio is Float32 at the
// device rate — roughly 190 KB per second — so an utterance that never closes (a stuck capture, a
// room that never falls quiet) would grow without bound and eventually take the tab down. Five
// minutes is ~57 MB, far beyond any real sentence, and long audio is handled properly now: the
// worker chunks anything over 30 s WITH timestamps, which is what stops it being cut in half.
export const MAX_UTTERANCE_MS = 300000
// The floor needs a moment to learn the room before it can judge anything. Without this it starts
// at a hardcoded guess, and any background above that guess opens an utterance instantly — with
// music playing, the music itself triggered capture within 85 ms and then held the level above the
// frozen threshold, so the utterance never closed and ran to MAX_UTTERANCE_MS every time.
export const WARMUP_MS = 600
// Trailing silence is trimmed off before the clip goes to Whisper: at a 5 s threshold it would
// otherwise be most of the audio, which costs transcription time on a slow CPU and invites the
// well-known failure where Whisper invents text to fill a long silence. A little is kept so the
// final word is not clipped.
export const KEEP_TAIL_MS = 300

/**
 * Milliseconds to blocks, against the device's ACTUAL sample rate.
 *
 * A block is 2048 frames: 42.7 ms at 48 kHz but 46.4 ms at 44.1 kHz. Thresholds written as block
 * counts therefore mean different wall-clock times per machine, which is a poor way to express
 * "wait about a second and a half". Converted once at start-up — the audio callback runs hundreds
 * of times a second and should not be doing arithmetic on constants.
 */
export function blockTiming(sampleRate, blockSize = 2048) {
  const msPerBlock = (blockSize / sampleRate) * 1000
  return { msPerBlock, inBlocks: (ms) => Math.max(1, Math.round(ms / msPerBlock)) }
}

/**
 * Continuous segmentation (VoiceLive.jsx): cut the room into utterances.
 *
 * The caller owns the audio buffer — it is the only part that needs real samples — and tells this
 * how many blocks it is holding. `step` returns what to do with them:
 *
 *   { state: "idle" }       nothing open; keep only the last `prerollBlocks` so the first word of
 *                           whatever comes next survives the detection delay
 *   { state: "opened" }     an utterance just started; the preroll is now part of it
 *   { state: "capturing" }  keep buffering
 *   { state: "closed", dropTrailing, spokenBlocks, tooLong, usable }
 *                           done: drop `dropTrailing` blocks off the end (trailing silence, minus
 *                           a short tail so the last word is not clipped) and, if `usable`, send
 *                           the rest. `usable` false means a cough or a click, not a sentence.
 *
 * `endBlocks` and `maxBlocks` are per-step because they differ by mode: while armed for a wake
 * phrase the pause is much tighter and the cap much shorter than in conversation.
 */
export function createUtteranceGate({ prerollBlocks, startBlocks, warmupBlocks, minBlocks,
                                      keepTailBlocks, floor = createNoiseFloor() }) {
  let capturing = false
  let voiced = 0
  let quiet = 0
  let spoken = 0
  const IDLE = { state: "idle" }
  return {
    get capturing() { return capturing },
    get prerollBlocks() { return prerollBlocks },
    /**
     * Abandon whatever was open. Used after Jarvis has spoken, so the tail of its own voice
     * coming back through the room is not mistaken for the next thing said to it.
     *
     * Deliberately does NOT reset the noise floor: the room is the same room, and making it
     * re-learn from scratch would re-open the warm-up window after every single reply.
     */
    reset() {
      capturing = false
      voiced = 0
      quiet = 0
      spoken = 0
    },
    step(level, bufferedBlocks, { endBlocks, maxBlocks }) {
      const { loud } = floor.update(level)
      if (!capturing) {
        // Nothing may open an utterance until the floor has had time to learn the room.
        if (floor.blocks < warmupBlocks) {
          voiced = 0
          return IDLE
        }
        voiced = loud ? voiced + 1 : 0
        if (voiced < startBlocks) return IDLE
        capturing = true
        quiet = 0
        spoken = voiced          // the blocks that opened it were speech too
        return { state: "opened" }
      }
      quiet = loud ? 0 : quiet + 1
      if (loud) spoken++
      const tooLong = bufferedBlocks >= maxBlocks
      if (quiet < endBlocks && !tooLong) return { state: "capturing" }
      const dropTrailing = Math.max(0, quiet - keepTailBlocks)
      const spokenBlocks = spoken
      capturing = false
      voiced = 0
      quiet = 0
      spoken = 0
      // Measured in VOICED blocks, not buffered ones. Every utterance ends with the full pause by
      // construction, and that alone is several times the minimum — so testing the buffer length
      // made this guard unreachable, and a cough went to Whisper as a turn.
      return { state: "closed", dropTrailing, spokenBlocks, tooLong, usable: spokenBlocks >= minBlocks }
    },
  }
}
