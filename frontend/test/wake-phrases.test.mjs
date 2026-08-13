/**
 * wake-phrases — recognising an address to Jarvis from the transcript.
 *
 * This is the half of wake detection that does NOT need a trained model, so it is the half that
 * decides whether "jarvis, are you there" works at all. It also runs on every short utterance in
 * the room while armed, which makes false positives a real cost rather than a curiosity.
 */
import assert from "node:assert/strict"
import test from "node:test"

import { readFileSync } from "node:fs"

import {
  DEFAULT_WAKE_PHRASES, GREETINGS, NOISE, isGreetingRemainder, matchWakePhrase, normalise,
  parsePhraseList,
} from "../src/wake-phrases.js"

test("the phrasings that prompted this all wake it", () => {
  for (const said of [
    "Hey Jarvis",
    "Jarvis, are you there?",
    "jarvis are you there",
    "Wake up, Jarvis!",
    "Jarvis, wake up.",
    "OK Jarvis",
    "Okay Jarvis.",
    "Jarvis",
  ]) {
    assert.equal(matchWakePhrase(said).matched, true, `should have woken on: ${said}`)
  }
})

test("what you said after the wake phrase is carried through", () => {
  const m = matchWakePhrase("Jarvis, what's the weather today?")
  assert.equal(m.matched, true)
  assert.equal(m.remainder, "what's the weather today?",
    "the command keeps its punctuation — it is what gets sent to the model")
})

test("the longest matching phrase wins", () => {
  // "jarvis are you there" contains "jarvis". Matching the short one would leave the remainder as
  // "are you there" — a command to answer rather than a greeting to acknowledge.
  const m = matchWakePhrase("jarvis are you there")
  assert.equal(m.phrase, "jarvis are you there")
  assert.equal(m.remainder, "")
})

test("a little filler before the name is tolerated", () => {
  assert.equal(matchWakePhrase("um, ok so jarvis").matched, true)
})

test("the name buried mid-sentence does NOT wake it", () => {
  // Everything short said in the room reaches this while armed, so this is the difference between
  // a wake word and an assistant that interrupts your conversation about it.
  for (const said of [
    "I was telling Sam about Jarvis yesterday",
    "the thing is that my jarvis setup keeps crashing",
  ]) {
    assert.equal(matchWakePhrase(said).matched, false, said)
  }
})

test("it matches whole words, not fragments", () => {
  assert.equal(matchWakePhrase("jarvisware is a product").matched, false)
  assert.equal(matchWakePhrase("what does jarvism mean").matched, false)
})

test("ordinary conversation does not wake it", () => {
  for (const said of ["what's the weather", "turn off the light", "hello there", "", "   "]) {
    assert.equal(matchWakePhrase(said).matched, false, JSON.stringify(said))
  }
})

test("a bare wake phrase and a pleasantry are both greetings, a request is not", () => {
  assert.equal(isGreetingRemainder(""), true, "the name alone is an address, not a question")
  assert.equal(isGreetingRemainder("are you there"), true)
  assert.equal(isGreetingRemainder("you there"), true)
  assert.equal(isGreetingRemainder("good morning"), true)
  assert.equal(isGreetingRemainder("what's the weather"), false)
  assert.equal(isGreetingRemainder("turn off the light"), false)
})

test("normalise strips what Whisper adds and nothing else", () => {
  assert.equal(normalise("  Hey, JARVIS!!  "), "hey jarvis")
  assert.equal(normalise("Jarvis... are   you there?"), "jarvis are you there")
})

test("a user-edited list is parsed, de-duplicated, and never left empty", () => {
  assert.deepEqual(parsePhraseList("Hey Jarvis, computer\nJARVIS"), ["hey jarvis", "computer", "jarvis"])
  assert.deepEqual(parsePhraseList("   "), DEFAULT_WAKE_PHRASES)
  assert.deepEqual(parsePhraseList(null), DEFAULT_WAKE_PHRASES)
})

test("a custom list replaces the defaults rather than adding to them", () => {
  const only = ["computer"]
  assert.equal(matchWakePhrase("computer, are you there", only).matched, true)
  assert.equal(matchWakePhrase("hey jarvis", only).matched, false,
    "someone who set their own wake word should not still answer to the old one")
})


test("the remainder is sliced from the original, not the normalised text", () => {
  assert.equal(matchWakePhrase("Jarvis, don't turn off the light!").remainder,
               "don't turn off the light!")
  assert.equal(matchWakePhrase("Hey Jarvis - what's 2+2?").remainder, "what's 2+2?")
})


// --- the greeting contract, shared with the server ------------------------------------------
// config/greeting_phrases.json is the single definition of "this utterance is only a greeting".
// It exists because this file's list and the server's had drifted apart without anyone seeing it:
// the server answered "how are you" with "Yes, sir." because "how" is a prefix of "howdy", and the
// box microphone dropped "Jarvis, hit the lights" because "hit" starts with "hi". Both sides keep
// their list as a literal — the browser must not fetch a file to answer "hello" — so the fixture
// pins them from the outside, and the matching assertions run in pytest too.

const FIXTURE = JSON.parse(
  readFileSync(new URL("../../config/greeting_phrases.json", import.meta.url), "utf8"))

test("the browser's greeting list matches the shared fixture", () => {
  assert.deepEqual([...GREETINGS].sort(), [...FIXTURE.phrases].sort())
  assert.deepEqual([...NOISE].sort(), [...FIXTURE.noise].sort())
})

test("every listed phrase is answered without the model", () => {
  for (const phrase of [...FIXTURE.phrases, ...FIXTURE.noise]) {
    assert.equal(isGreetingRemainder(phrase), true, `${phrase} should be a greeting`)
  }
})

test("every regression phrase reaches the model", () => {
  for (const phrase of FIXTURE.not_greetings) {
    // The fixture holds whole utterances; this function sees what follows the wake phrase, so
    // strip it the way matchWakePhrase would.
    const remainder = matchWakePhrase(phrase).remainder ?? phrase
    assert.equal(isGreetingRemainder(remainder || phrase), false,
      `${phrase} carries a question or a command and must not be swallowed`)
  }
})

test("a request that merely starts with a greeting is not a greeting", () => {
  // The old rule matched any remainder STARTING with a greeting, so this was a pleasantry.
  assert.equal(isGreetingRemainder("hey turn on the light"), false)
  assert.equal(isGreetingRemainder("hello can you set a timer"), false)
})
