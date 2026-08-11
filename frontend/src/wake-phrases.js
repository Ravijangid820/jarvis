/**
 * wake-phrases.js — recognising that you were talking to Jarvis, from the transcript.
 *
 * The neural spotter (wake-detect.js) is a CLASSIFIER TRAINED ON ONE PHRASE. openWakeWord ships
 * exactly six pre-trained models — alexa, hey_jarvis, hey_mycroft, hey_rhasspy, timer, weather —
 * so "jarvis, are you there" cannot be added to it by configuration; it would need its own trained
 * model. This module is the other half of the answer: while armed, short utterances are transcribed
 * and matched against an editable list, so any phrasing works today without training anything.
 *
 * The two run together. "Hey Jarvis" still fires instantly through the spotter, without waiting for
 * a pause or transcribing anything; everything else lands here a fraction of a second later.
 *
 * Pure functions only — no DOM, no workers — so the matching rules can be exercised under
 * `node --test` rather than by talking to a laptop.
 */

/** Sensible defaults. Users can edit the list; these are what the box answers to out of the box. */
export const DEFAULT_WAKE_PHRASES = [
  "hey jarvis",
  "ok jarvis",
  "okay jarvis",
  "jarvis",
  "wake up jarvis",
  "jarvis wake up",
  "jarvis are you there",
  "you there jarvis",
]

/**
 * How far into the utterance a wake phrase may begin, in words.
 *
 * Not zero: people prefix real addresses with filler ("um, jarvis…", "ok so jarvis…"). Not
 * unlimited either, or "I was telling Sam about Jarvis yesterday" wakes it mid-conversation —
 * which matters here in a way it does not on the server, because while armed EVERY short utterance
 * in the room reaches this function, not just ones aimed at the assistant.
 */
export const MAX_PREFIX_WORDS = 3

/** Lowercase, strip punctuation, collapse whitespace. Whisper's casing and commas are not signal. */
export function normalise(text) {
  return (text || "")
    .toLowerCase()
    .replace(/[^a-z0-9\s]+/g, " ")
    .split(/\s+/)
    .filter(Boolean)
    .join(" ")
}

/**
 * Did this utterance address Jarvis, and what did it go on to say?
 *
 * Longest phrase first, so "jarvis are you there" is preferred over the bare "jarvis" it contains —
 * otherwise the remainder would come back as "are you there" and be treated as a command rather
 * than recognised as the greeting it is.
 *
 * @returns {{matched: boolean, phrase: string, remainder: string}}
 */
export function matchWakePhrase(text, phrases = DEFAULT_WAKE_PHRASES) {
  const src = text || ""
  // Words with their offsets in the ORIGINAL string. Matching happens on the normalised form, but
  // the remainder is sliced from the original — it becomes the user's actual command, and
  // "what's the weather" must not reach the model as "what s the weather".
  const tokens = []
  for (const m of src.matchAll(/[A-Za-z0-9]+/g)) {
    tokens.push({ word: m[0].toLowerCase(), start: m.index, end: m.index + m[0].length })
  }
  if (!tokens.length) return { matched: false, phrase: "", remainder: "" }

  const ordered = [...new Set(phrases.map(normalise).filter(Boolean))]
    .sort((a, b) => b.split(" ").length - a.split(" ").length || b.length - a.length)

  for (const phrase of ordered) {
    const pWords = phrase.split(" ")
    // Word positions rather than substring search, so "jarvis" does not fire inside "jarvisware"
    // and the remainder always begins on a word boundary.
    for (let i = 0; i <= tokens.length - pWords.length && i <= MAX_PREFIX_WORDS; i++) {
      if (pWords.every((w, j) => tokens[i + j].word === w)) {
        const after = tokens[i + pWords.length]
        const remainder = after ? src.slice(after.start).trim().replace(/^[,.:;!?\s-]+/, "") : ""
        return { matched: true, phrase, remainder }
      }
    }
  }
  return { matched: false, phrase: "", remainder: "" }
}

/**
 * Is the remainder just a pleasantry rather than a request?
 *
 * Mirrors the server's list in voice_bridge.py, so "Jarvis, are you there" gets the same spoken
 * acknowledgement whether it was heard by the box's microphone or by a browser tab. Divergence
 * here would be invisible and maddening: the same words answered two different ways depending on
 * which microphone happened to be listening.
 */
const GREETINGS = [
  "hello", "hi", "hey", "good morning", "good afternoon", "good evening",
  "you there", "are you there", "there", "are you awake", "wake up", "you awake",
]

export function isGreetingRemainder(remainder) {
  const r = normalise(remainder)
  if (!r) return true                       // the wake phrase alone — acknowledge and listen
  return GREETINGS.some(g => r === g || r.startsWith(g + " "))
}

/** Parse a user-edited list ("hey jarvis, jarvis, wake up jarvis") into clean phrases. */
export function parsePhraseList(raw, fallback = DEFAULT_WAKE_PHRASES) {
  const list = [...new Set((raw || "").split(/[,\n]/).map(normalise).filter(Boolean))]
  return list.length ? list : [...fallback]
}
