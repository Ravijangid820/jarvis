/**
 * wake-phrases — recognising an address to Jarvis from the transcript.
 *
 * This is the half of wake detection that does NOT need a trained model, so it is the half that
 * decides whether "jarvis, are you there" works at all. It also runs on every short utterance in
 * the room while armed, which makes false positives a real cost rather than a curiosity.
 */
import assert from "node:assert/strict"
import test from "node:test"

import {
  DEFAULT_WAKE_PHRASES, isGreetingRemainder, matchWakePhrase, normalise, parsePhraseList,
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
