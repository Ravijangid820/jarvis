# Jarvis — Code & Security Audit

_Multi-agent audit (6 reviewers, adversarial verification), 2026-06-15._

**Confirmed findings: 81** — 1 critical · 14 high · 32 medium · 34 low. (6 additional findings were raised but rejected as false positives during verification.)

> Severity is post-verification (adjusted). Line numbers are best-effort against the audited revision.

> **A follow-up whole-project review (2026-06-17)** — covering the device/edge/agent subsystems
> added since — is appended at the **end of this file** (findings F1–F24, all OPEN for review).


## Security (13)

### 1. [HIGH] Orchestrator bound to 0.0.0.0:5000 over plaintext HTTP with no TLS

- **Location:** `jarvis.json:13 (host 0.0.0.0), service:12-13`
- **Problem:** The orchestrator listens on 0.0.0.0:5000 (jarvis.json line 13, uvicorn --host 0.0.0.0 in the service unit) with no TLS and no reverse proxy in front. All authentication is Bearer-token based (login token, API keys, and the master api_key) and is sent in cleartext over HTTP. Anyone on the same LAN/network path can sniff session tokens, API keys, and the master key, then fully impersonate users/admin. Bearer tokens are also long-lived (30 days) so a single capture is durable. Despite the project being described as single-user-ish self-hosted, binding to all interfaces exposes it beyond localhost.
- **Fix:** Bind to 127.0.0.1 and terminate TLS at a reverse proxy (caddy/nginx) with HTTPS, or run behind a VPN/WireGuard. If remote access is truly needed, require TLS so Bearer tokens are never transmitted in cleartext.

### 2. [HIGH] Master API key acts as a permanent admin (user_id=1) bypass with no expiry or revocation

- **Location:** `main.py:55,132-137; jarvis.json:2`
- **Problem:** MASTER_API_KEY is read from config and any Bearer token equal to it is granted request.state.user_id=1 and is_admin=True (lines 134-137), bypassing the sessions/api_keys tables entirely and skipping the rate limiter (the rate-limit check only runs for non-admins, lines 167-170). This single static secret grants full admin: create/delete users, mint API keys, read all stats, force fact extraction. It is stored in plaintext in config/jarvis.json (line 2), is also embedded into the voice listener curl command, never expires, and cannot be revoked without editing config and restarting. Compromise of the config file or any process that reads it (run_listener.sh) yields permanent admin. Combined with the plaintext-HTTP exposure above, the key can be sniffed off the wire.
- **Fix:** Treat the master key as bootstrap-only: disable it after first admin user is created, or gate it to localhost-only requests (check request.client.host). Rotate it out of plaintext config into a root-only file / secret store, and add an expiry/disable flag. Apply rate limiting to admin/master requests too.

### 3. [HIGH] Master API key committed/stored in plaintext config alongside the repo

- **Location:** `2`
- **Problem:** A real 64-hex-char api_key is stored in cleartext in config/jarvis.json. The git status shows config/ is an untracked directory about to be added; if committed, this admin-equivalent secret enters version history permanently. run_listener.sh also extracts it via python and bakes it into a shell command line, where it becomes visible in the process table (ps aux) to any local user.
- **Fix:** Move the secret out of the repo, ensure config/jarvis.json is gitignored, restrict file perms to the service user (chmod 600, root-owned), and avoid passing the key on a command line (it leaks via ps). Rotate the key since it has already been exposed in this file.

### 4. [MEDIUM] IDOR: session ownership checks fail-open when session row is missing or session_id='default'

- **Location:** `786-797 (history), 813-820 (inbox), 882-888 (stream)`
- **Problem:** Ownership is enforced with `row = SELECT user_id ...; if row and row['user_id'] != request.state.user_id: 403`. When the session does not exist, `row` is None and the check is skipped, so any authenticated user can write messages to / generate titles for an arbitrary attacker-chosen session_id (store_message inserts into conversation_history with that id; create_session is never required). Additionally, session_id 'default' is explicitly exempted from all ownership checks in /inbox, /chat/stream and /history, and conversation_history.session_id defaults to 'default' (schema line 12). All users therefore share one global 'default' conversation namespace: messages, history readback, and RAG/vector recall under 'default' leak across users. The retrieve_long_term_memory query does filter by user_id metadata, but get_recent_context(session_id) and /history/default do not, so any user reading session 'default' sees other users' default-session messages.
- **Fix:** Reject requests for sessions that do not belong to the caller (treat missing row as 403, not pass). Remove the 'default' special-case or scope it per-user (namespace as user_id). Verify session ownership before storing messages or generating titles.

### 5. [MEDIUM] Rate limiter is per-IP, in-memory, and fully bypassed for any admin/api-key/master token

- **Location:** `81-89, 167-170`
- **Problem:** check_rate_limit only runs for non-admin sessions (line 167). Any admin user, the master key, or an api_key tied to an admin-role user is exempt, so a compromised or shared admin/api key can flood the (single-worker, ~3.7 tok/s) LLM backend unbounded — a trivial DoS given the hardware. The limiter is keyed on request.client.host, which behind any proxy collapses all users to one IP (and without a proxy can be spoofed/rotated). It is also process-local and resets on restart, and the dict (_rate_store) grows unbounded per distinct IP (memory exhaustion vector). Login (/auth/login) has NO rate limiting at all, enabling unthrottled password brute-forcing.
- **Fix:** Apply a global/back-pressure limit on expensive LLM endpoints regardless of role (the backend can serve only one request anyway). Add explicit rate limiting to /auth/login (per-username and per-IP) to stop brute force. Evict stale IP buckets. If behind a proxy, derive client IP from a trusted X-Forwarded-For only.

### 6. [MEDIUM] Stored XSS risk: admin panel renders usernames, key descriptions and other user-controlled fields via innerHTML

- **Location:** `296-299 (users), 305-313 (modal), 343-347 (keys)`
- **Problem:** The admin panel builds table rows and the user-details modal with innerHTML using unescaped, attacker-influenced values: u.username, u.role, k.description, k.user_id, u.created_at, and full_key are interpolated directly into template-literal HTML (e.g. line 296: `...${u.username}...` injected via tr.innerHTML; line 343 same for k.description; modal at 305-313). A non-admin user who registers/sets a username containing markup, or whose api-key description is attacker-controlled, can store a payload that executes in the admin's browser when the admin opens /admin — i.e. stored XSS leading to admin-session theft (token is in localStorage). Note: usernames are created by admins here, but key descriptions and any future self-service username path make this exploitable, and the deleteKey/deleteUser onclick handlers also interpolate values into inline JS (line 299 deleteUser(${u.id}), 347 deleteKey('${k.full_key}')), which is an injection sink if those values were ever non-numeric/contained quotes.
- **Fix:** Render all dynamic values with textContent / createTextNode (as app.js correctly does), or HTML-escape before interpolation. Never build onclick handlers by string-concatenating data values; attach handlers via addEventListener and pass values through closures/dataset.

### 7. [LOW] CORS allow_origins='*' on a credentialed, token-authenticated API

- **Location:** `74-79`
- **Problem:** CORSMiddleware is configured with allow_origins=['*'] and allow_methods POST/GET/PUT/DELETE while allowing the Authorization header. Because auth uses a Bearer token in localStorage (not cookies), the classic credentialed-CORS escalation is limited, but a wildcard CORS policy still permits any website the victim visits to issue authenticated requests if it can obtain the token, and broadens the attack surface for any future cookie/credential use. For a self-hosted single-origin app there is no reason to allow arbitrary origins.
- **Fix:** Set allow_origins to the explicit served origin(s) only (e.g. the deployment host). Do not combine '*' with broad method/header allowances on an authenticated API.

### 8. [LOW] Knowledge edit/delete endpoints do not return 404 on cross-user IDs but rely solely on WHERE user_id scoping (acceptable) — however no existence/authorization feedback and FTS/vector data not cleaned

- **Location:** `332-357, 1101-1114`
- **Problem:** update_fact/delete_fact correctly scope by `WHERE id = ? AND user_id = ?`, so cross-user knowledge tampering is prevented (good). The residual issue is that they always return status ok even when nothing matched (no ownership signal), and there is no validation that fact_id belongs to the user beyond the silent WHERE clause. This is low risk but means a caller cannot distinguish 'not yours' from success. Not a privilege escalation.
- **Fix:** Optionally return 404 when rowcount is 0 so callers cannot probe, but the data-access scoping itself is correct. No urgent change required.

### 9. [LOW] Auth-session tokens never expire on logout server-side; 30-day lifetime; sessions table never purged

- **Location:** `748-764 (login), app.js:74 / App.jsx:104-111 (logout)`
- **Problem:** Login issues a 256-bit token (good entropy) with a 30-day expiry. Logout only removes the token from localStorage client-side (clearAuth / doLogout); there is no /auth/logout endpoint deleting the row from auth_sessions, so a token captured before logout remains valid for up to 30 days. Expired rows are also never deleted, so the table grows indefinitely and the master-key path means there is no way to globally invalidate sessions. Tokens are stored in localStorage, which is readable by any XSS on the page.
- **Fix:** Add a server-side logout that deletes the auth_sessions row, periodically purge expired sessions, and consider shorter token lifetimes with refresh. Storing tokens in localStorage is acceptable only if XSS is fully prevented (see XSS findings).

### 10. [LOW] Admin page authorizes purely on client-side localStorage role flag; admin endpoints rely on server check (server OK) but admin HTML is publicly served

- **Location:** `main.py:1062-1065 (/admin no auth), admin.html:261-262, App.jsx:378`
- **Problem:** GET /admin is in the middleware allow-list (line 115) and serves admin.html to anyone unauthenticated. The 'Admin' button is shown based on localStorage jarvis_role==='admin' (App.jsx 378, app.js 316), which a user can set themselves, but that only reveals the UI. The actual data endpoints (/admin/*) correctly re-check request.state.is_admin server-side (e.g. lines 938, 951, 970, 1007), so privilege is enforced at the API. The residual concern is purely that the admin template and its JS are exposed; no data leaks without a valid admin token. Calling this out so it is not mistaken for an enforcement gap.
- **Fix:** Low priority: optionally gate /admin behind the auth middleware too, but the server-side is_admin checks are the real control and they are present. Do not rely on the client role flag for anything security-relevant (it already isn't).

### 11. [LOW] verify_password uses bare except masking errors; login enumeration and missing constant-time username lookup

- **Location:** `99-105, 748-755`
- **Problem:** Password verification uses PBKDF2-HMAC-SHA256 with 100k iterations and secrets.compare_digest (good). However verify_password wraps everything in a bare `except:` returning False, which would also swallow unexpected errors. More notably, login returns 401 'Invalid username or password' uniformly (good) but performs no password hashing when the username is missing, creating a timing side-channel that can distinguish valid vs invalid usernames, and there is no rate limiting on login (see rate-limit finding), making username enumeration + brute force feasible.
- **Fix:** Always run a dummy PBKDF2 verification on unknown usernames to equalize timing, replace bare except with `except Exception`, and rate-limit login attempts.

### 12. [LOW] system_prompt override and parameters are fully client-controlled for all users

- **Location:** `725-738, 691-720, 824/892`
- **Problem:** QueryRequest accepts system_prompt and all sampling params from any authenticated user, and build_messages uses the client-supplied system_prompt verbatim (line 692). A normal user can override the system prompt and sampling, jailbreaking the assistant persona and potentially extracting other injected context. The max_length on text is set to ADMIN_MAX_INPUT (10000) at the model level, then re-checked to 500 for non-admins at runtime (lines 810/879) — workable, but system_prompt itself has no length bound and n_predict is unbounded, allowing a user to request very large generations (DoS on the 3.7 tok/s backend).
- **Fix:** Bound n_predict and system_prompt length server-side; consider restricting system_prompt override to admins. Treat this as low severity for a self-hosted single-user deployment.

### 13. [LOW] _mark_messages_processed / fact extraction attribute messages to user via default user_id=1 on legacy/default sessions

- **Location:** `360-377, 400-455, 502-503`
- **Problem:** Background fact extraction joins conversation_history to chat_sessions and defaults user_id to 1 when the session has no owner (m.get('user_id') or 1, line 408; store_message also defaults user_id to 1 at line 503). Because 'default' session messages from any user share session_id 'default' and that row may map to user_id 1 (the schema ALTER sets DEFAULT 1), personal facts extracted from one user's 'default' conversation can be stored under user 1's knowledge base and later injected into user 1's system prompt. Combined with the shared-default IDOR above, this can leak one user's personal facts into another user's profile.
- **Fix:** Eliminate the shared 'default' session (namespace per user). Do not default unknown ownership to user_id 1; skip extraction when ownership is unknown.


## Correctness / Bugs (12)

### 14. [CRITICAL] Context window (-c 1024) far smaller than the context the orchestrator builds (up to 100 history messages + knowledge + RAG + 1024 max_tokens)

- **Location:** `llama-fast.service:10 (-c 1024); main.py:62,514-522,691-720; jarvis.json:20 (max_context_messages=100)`
- **Problem:** llama-server is launched with -c 1024 (total KV-cache context = 1024 tokens). The orchestrator's build_messages() injects: the system prompt (~40 tokens), the entire USER PROFILE knowledge block (get_user_knowledge returns ALL facts, unbounded), a RECALLED MEMORIES RAG block (up to 5 docs), get_recent_context() which pulls MAX_CONTEXT_MESSAGES=100 prior messages, plus the new user turn. On top of that the frontend sends n_predict=1024 (App.jsx:39) which maps to max_tokens=1024. Prompt + requested completion will routinely exceed 1024 tokens. llama.cpp will either truncate the prompt (silently dropping the oldest context, including the system prompt or the actual user question depending on ordering) or the generation gets squeezed to almost nothing because the prompt already consumes the whole window. Either way the model loses the system prompt / knowledge / current question. This is the single most impactful functional bug: on any non-trivial conversation the assistant effectively forgets context or returns near-empty output.
- **Fix:** Either raise -c substantially (e.g. 4096+ to match max_context_tokens=4096 already declared in jarvis.json) or drastically cap what build_messages injects: enforce a token budget, lower max_context_messages (100 is absurd for a 1024 window), and clamp n_predict so prompt_tokens + n_predict <= context. Compute remaining = ctx - prompt_tokens and set max_tokens accordingly.

### 15. [HIGH] voice_feedback flag is silently ignored by /chat/stream (the only endpoint the web UI uses)

- **Location:** `main.py:872-928 (chat_stream never reads request.voice_feedback); App.jsx:212,218`
- **Problem:** The QueryRequest model defines voice_feedback (main.py:738) and the React UI exposes a 'Voice Output' toggle that sets payload.voice_feedback=voice (App.jsx:212,437). But the UI sends every message to /chat/stream (App.jsx:218), and chat_stream() never references request.voice_feedback and never invokes piper. Only /inbox (the voice-listener path) honors voice_feedback (main.py:844-855). Result: the user can toggle Voice Output on in the UI and nothing ever happens; no audio is produced and there is no error. The feature is dead in the web client.
- **Fix:** Either remove the toggle from the UI, or implement TTS in the streaming path. Note TTS over SSE is awkward (you'd buffer the full answer then emit a final audio event); a cleaner option is to emit the base64 audio in the final 'done' SSE frame the way /inbox returns it in JSON.

### 16. [HIGH] Fact-extraction misattributes messages from 'default' sessions to user 1 via LEFT JOIN

- **Location:** `main.py:360-377 (LEFT JOIN), 408-409 (uid = m.get('user_id') or 1)`
- **Problem:** _get_unprocessed_messages LEFT JOINs conversation_history to chat_sessions on session_id. Messages stored under session_id='default' (the schema default, and the value used before a real session is created) have NO matching chat_sessions row, so cs.user_id comes back NULL. In _extract_facts_batch, `uid = m.get('user_id') or 1` then attributes every NULL-user message to user_id 1 (the admin). Any non-admin user whose messages landed in a 'default' session (e.g. the voice /inbox path which sends no session_id, defaulting to 'default', or the first message before session creation) will have their personal facts written into the admin's user_knowledge. This is a cross-user data leak: another user's facts pollute admin's persistent profile and will be injected into admin's prompts. Even single-user, voice-path facts always get filed under user 1 regardless of who spoke.
- **Fix:** Do not silently coerce NULL/missing user_id to 1. Skip messages whose session has no owner, or resolve the real owner. Better: stop using 'default' as a persisted session_id for stored conversation, or backfill user_id onto conversation_history at write time so attribution does not depend on a join that can miss.

### 17. [HIGH] Streaming LLM errors are stored as the assistant's reply instead of being surfaced as an error

- **Location:** `main.py:615-617 (yields '<ERROR: AI backend error>' as content), 902-911`
- **Problem:** request_llm_stream catches any exception (connection refused, timeout, llama-server crash) and yields the literal string '<ERROR: AI backend error>' as a normal content chunk (line 617). In chat_stream's event_generator, that string is appended to full_answer and streamed to the client as if it were model output, AND because answer_text is now non-empty, store_message persists '<ERROR: AI backend error>' as the jarvis turn in conversation_history (lines 908-911) and into ChromaDB. The error sentinel pollutes history and RAG, and the frontend renders it as a normal assistant message (App.jsx:246-252) with no error styling. The separate `{'error': ...}` SSE frame (line 906) is only emitted for exceptions raised in the generator loop itself, which won't happen because request_llm_stream swallows them internally and converts them to content.
- **Fix:** Have request_llm_stream signal failure out-of-band (e.g. raise, or yield a typed marker the generator recognizes). In event_generator, detect failure, emit a proper {'error': ...} SSE frame, and do NOT store the error text as a message. The frontend should also render data.error.

### 18. [MEDIUM] On client disconnect or any failure, the user's message is never stored (one-sided loss) — and conversely partial answers are stored without consistency

- **Location:** `main.py:894-911`
- **Problem:** In chat_stream, both the user message and the assistant message are stored only AFTER streaming completes, inside event_generator, guarded by `if answer_text:` (line 909). If the model yields nothing (empty answer), or the client disconnects before generation starts, or the LLM 503s before producing any content, the user's own message is silently dropped from history. The user turn is not persisted independently of the assistant turn. So a failed turn leaves no record at all, and the next request's get_recent_context will not include the question the user asked. /inbox (line 857-858) has the inverse-but-related issue: it stores both unconditionally even though answer could be empty, but at least keeps the user turn. The streaming path's all-or-nothing behavior tied to a non-empty answer is the riskier one.
- **Fix:** Store the user message immediately when the request is accepted (before streaming), and store the assistant message separately when/if it completes. Consider handling client disconnect (request.is_disconnected / generator GeneratorExit) to still persist whatever partial answer was produced.

### 19. [MEDIUM] needs_title computed from existing_context can be wrong, and title generation issues a 3rd serialized LLM call on a single-threaded backend

- **Location:** `main.py:890-891, 913-921 (stream); 822-823, 860-868 (inbox)`
- **Problem:** needs_title is computed as len(existing_context)==0 BEFORE the user/assistant turns are stored. In chat_stream this is fine for the first turn, but title generation runs a separate blocking request_llm call (line 916) AFTER the main streaming completed. With llama-server --parallel 1 (llama-fast.service:14) and ~3.7 tok/s, this serializes a second full inference (up to 10 tokens, but with prompt processing) before the 'done' frame is sent, delaying completion noticeably and holding the single LLM slot. Additionally, get_recent_context (used for existing_context, line 890) uses MAX_CONTEXT_MESSAGES=100 just to test emptiness — cheap, but if the very first user message failed to store on a prior attempt (see message-persistence finding), needs_title can re-trigger title generation on a later turn, overwriting a user-renamed title. Title gen also has no length cap on the result beyond the model's n_predict=10, and strips quotes/periods but not newlines.
- **Fix:** Generate the title before or concurrently with the main answer, or skip the extra LLM round-trip and derive a title from the first user message. Guard title regeneration so it never overwrites a manually-set title. Strip/whitespace-normalize the generated title.

### 20. [MEDIUM] _mark_messages_processed marks unrelated/un-extracted messages as processed, silently skipping their facts

- **Location:** `main.py:379-398, 454-455`
- **Problem:** After extracting from a batch of up to 20 user messages, _mark_messages_processed runs a second UPDATE that sets facts_extracted=1 for EVERY message (facts_extracted=0) in ANY session that contained one of the batched messages (the subquery selects DISTINCT session_id for the batched ids, then marks all unprocessed rows in those sessions). The intent stated in the comment is 'also mark the corresponding jarvis responses', but the query is not limited to jarvis rows or to messages older than the batch — it also marks user messages in those sessions that were NOT part of this batch (e.g. messages 21+ in a busy session, or new user messages that arrived after the batch was read). Those user messages never go through fact extraction, so their facts are permanently lost. This is a silent data-loss bug that worsens with conversation volume per session.
- **Fix:** Restrict the second UPDATE to speaker='jarvis' and to ids <= max(batched id) within those sessions, or simpler: mark exactly the batched user ids processed and handle jarvis rows by not selecting them for extraction in the first place (the WHERE already filters speaker='user'), so the second UPDATE is unnecessary and harmful — remove it.

### 21. [LOW] Idle memory worker can fire LLM fact-extraction concurrently with an active (slow) streaming request, contending for the single LLM slot

- **Location:** `main.py:222-224, 457-489, 804-805/874-875 (_update_activity only on entry)`
- **Problem:** _update_activity() is called only at the START of /inbox and /chat/stream (lines 805, 874), never on completion. At ~3.7 tok/s a long answer (e.g. 600 tokens) takes ~160s — longer than IDLE_THRESHOLD_SECONDS=120. So while a single request is still streaming, the background _memory_worker can decide the system is idle (idle_duration > 120) and call request_llm for fact extraction (line 422). With llama-server --parallel 1, both requests serialize on the backend: the user's stream stalls while the extraction inference runs, or vice versa. _last_activity_time is a plain module global mutated without a lock; on a single uvicorn worker with the GIL the assignment is atomic, but the read-modify-decision in the worker can still interleave with a request that just started. The worker also re-reads unprocessed messages and could double-process if extract-now is invoked simultaneously (no lock around _extract_facts_batch).
- **Fix:** Update activity timestamp on request completion too (or treat any in-flight request as active via a counter). Serialize LLM access with a lock so the idle worker never competes with live requests. Guard _extract_facts_batch / force_extraction with a mutex to prevent concurrent double-processing.

### 22. [LOW] store_message reuses the same connection for a slow ChromaDB embedding insert, and a stale local user_id lookup

- **Location:** `main.py:492-512`
- **Problem:** store_message commits the SQLite row, then while the connection is still open performs memory_collection.add(), which runs the SentenceTransformer (google/embeddinggemma-300m) embedding on CPU — a heavy, multi-hundred-ms operation on a Sandy Bridge i5 with no AVX2. This holds the open sqlite connection (in WAL mode) for the duration and runs synchronously inside the request/generator, adding latency to every stored turn. Also the user_id lookup (line 502) selects from chat_sessions, which for session_id='default' returns no row and defaults to user 1, mirroring the misattribution problem in the RAG metadata (vectors for default-session messages are tagged user_id=1).
- **Fix:** Move embedding/vector insert off the request path (queue/async), and close the SQLite connection before embedding. Resolve user_id consistently rather than defaulting to 1 for ownerless sessions.

### 23. [LOW] RAG msg_id lookup uses documents.index(doc), which returns the wrong index on duplicate document text

- **Location:** `main.py:649-654`
- **Problem:** Inside retrieve_long_term_memory, to find the id for a given doc it does idx = results['documents'][0].index(doc). list.index returns the FIRST matching position, so when two retrieved documents have identical text (common for short messages like 'ok', 'yes', 'thanks'), the wrong id is paired with the doc. This breaks the recent_context_ids dedup check (line 654): a past-session duplicate could be skipped or kept incorrectly. It does not corrupt data but undermines the dedup-against-recent-context logic the function exists for.
- **Fix:** Iterate with enumerate over the zipped lists and use the loop index directly instead of list.index(doc).

### 24. [LOW] Frontend can send n_predict/seed as NaN, producing invalid request bodies

- **Location:** `App.jsx:429,433,211`
- **Problem:** Max Tok and Seed are number inputs parsed with parseInt(e.target.value) (lines 429, 433). Clearing the field yields '' -> parseInt('') = NaN, which is stored in nPredict/seed and serialized into the payload (line 211). JSON.stringify turns NaN into null, so n_predict/seed arrive as null (handled as Optional, fine) — but an intermediate non-empty-but-invalid value, or a partially typed '-' for seed, yields NaN/odd values. Minor, but the seed=-1 default (line 40) is also passed through to llama as max_tokens-style seed; -1 is a valid 'random' sentinel for llama but the orchestrator forwards it verbatim with no validation.
- **Fix:** Guard parseInt results (fallback to defaults on NaN) and validate seed/n_predict ranges before sending.

### 25. [LOW] needs_title gate and ownership check both query chat_sessions for session_id != 'default', but 'default' sessions bypass ownership entirely

- **Location:** `main.py:814-820, 882-888, 788-797`
- **Problem:** Ownership verification in /inbox, /chat/stream and /history only runs when session_id != 'default' (lines 814, 882, 788). Any authenticated user can post to or read the shared 'default' session, and its stored messages are globally readable via /history/default and feed everyone's get_recent_context for that id. Combined with the fact-attribution-to-user-1 issue, the 'default' session is effectively a shared, mis-owned bucket. In a genuinely single-user deployment this is benign, but the codebase ships multi-user auth, so it is a real cross-user boundary gap.
- **Fix:** Either forbid the literal 'default' session for persisted history (require a real per-user session), or namespace default per user (e.g. session id derived from user_id) and apply ownership checks uniformly.


## Memory / RAG (12)

### 26. [HIGH] Embedding every message with torch/sentence-transformers is very heavy on a no-AVX2 Sandy Bridge CPU

- **Location:** `208, 505, 857-858`
- **Problem:** Every store_message() call embeds the document synchronously inside the /inbox and /chat/stream request path (lines 857-858 -> store_message -> memory_collection.add). The model is google/embeddinggemma-300m (~300M params) running via sentence-transformers on torch 2.12.0 CPU (confirmed in uv.lock). On an i5-2520M (2C/4T, no AVX2, 8GB RAM) torch falls back to non-AVX2 kernels and a 300M-param forward pass per message is slow (likely hundreds of ms to seconds each) and memory-hungry. Two messages are embedded per turn (user + jarvis). Combined with llama at ~3.7 tok/s and 8GB RAM shared with the 2B model, this adds notable per-turn latency and RAM pressure (torch + model weights can be ~1-1.5GB resident). retrieve_long_term_memory also embeds the query on the request path. Nothing is batched or backgrounded.
- **Fix:** (1) Use a much smaller embedding model (e.g. all-MiniLM-L6-v2, 22M params, ~5-10x faster, no prompt prefixes needed). (2) Move embedding off the request path — enqueue messages and embed in the existing background memory worker thread, or at least embed the assistant reply after responding. (3) Consider only embedding user messages (and important facts), not every jarvis reply, to halve the work. (4) Confirm torch isn't spawning more threads than cores; pin OMP/torch threads to 2.

### 27. [HIGH] _mark_messages_processed marks ENTIRE sessions processed, skipping fact extraction on later messages

- **Location:** `379-398, 360-377`
- **Problem:** After extracting from a batch, _mark_messages_processed not only sets facts_extracted=1 on the batch IDs but also runs a second UPDATE that sets facts_extracted=1 for ALL rows in any session touched by the batch (lines 388-393: 'WHERE facts_extracted=0 AND session_id IN (sessions of batch)'). The stated intent is to mark the jarvis responses, but it also marks any user messages in those sessions that were not yet extracted — including messages that arrive later but happened to be in the same session, and any user messages beyond the batch_size=20 limit. Because _get_unprocessed_messages only selects speaker='user' (line 368), unprocessed user messages in an active session get silently flagged as extracted without ever being sent to the LLM, permanently losing their facts. Long-running single-session usage (the expected pattern for this single-user box, default session) is exactly the worst case.
- **Fix:** Drop the blanket session-wide UPDATE. If you only need to avoid re-processing jarvis turns, simply never select them (already the case) — there's no need to mark them at all. Mark only the exact batch IDs that were processed. If you want jarvis context, fetch it for the prompt but mark only user-message IDs.

### 28. [MEDIUM] ChromaDB distance is L2 (squared), not cosine — RAG threshold 1.5 is mis-calibrated

- **Location:** `209, 215, 646`
- **Problem:** The collection is created with get_or_create_collection(name='jarvis_memory', embedding_function=emb_fn) with NO metadata={'hnsw:space': ...}. ChromaDB's default space is 'l2' (squared Euclidean), not cosine. The code comment on line 215 explicitly says 'Discard results with distance > this (cosine)' and the log message on line 668 reinforces it, but the distances returned by query() are squared L2 distances. For L2-normalized embeddings (sentence-transformers normalizes by default), squared-L2 = 2*(1-cosine_sim), so the usable range is 0..4, not 0..2. A threshold of 1.5 in squared-L2 corresponds to cosine similarity of ~0.25 — i.e. it admits fairly weak matches, the opposite of the 'discard irrelevant' intent. The threshold was almost certainly tuned mentally against a cosine scale (0..2) that doesn't apply here. This silently degrades retrieval precision.
- **Fix:** Explicitly set the space when creating the collection: get_or_create_collection(name='jarvis_memory', embedding_function=emb_fn, metadata={'hnsw:space':'cosine'}). Then a threshold near 0.35-0.5 means 'cosine distance', matching the comment. Alternatively keep L2 but recompute the threshold against the 0..4 squared-L2 range and fix the comments/logs. Verify empirically with a few known queries.

### 29. [MEDIUM] embeddinggemma-300m used without its required task prompt prefixes — large retrieval-quality loss

- **Location:** `208, 505, 626`
- **Problem:** google/embeddinggemma-300m is an asymmetric, instruction/prompt-conditioned model: queries must be embedded with a 'task: search result | query: {text}' style prefix and documents with a 'title: none | text: {text}' prefix (the model card specifies these prompt templates). Here both documents (store_message, line 505) and queries (retrieve_long_term_memory, line 626) are passed as raw text through ChromaDB's default SentenceTransformerEmbeddingFunction, which applies no such prefixes. Without the matching query/document prompts, embeddinggemma's retrieval quality drops substantially versus its benchmarked numbers — query and document vectors live in mismatched regions of the space. This compounds with the L2/threshold issue.
- **Fix:** Either wrap the embedding function to apply the model's query vs document prompt templates (separate encode paths for add vs query), or switch to a symmetric model (e.g. all-MiniLM-L6-v2) that needs no prefixes and is far cheaper on CPU. Given the hardware, a small symmetric model is the pragmatic choice.

### 30. [MEDIUM] store_fact word-overlap dedup heuristic is unreliable — both false merges and missed duplicates

- **Location:** `301-318`
- **Problem:** Dedup uses set(words) Jaccard-style overlap = |A∩B| / max(|A|,|B|) > 0.6 within the same category. Because all extracted facts start with the boilerplate 'The user ...' (per FACT_EXTRACTION_PROMPT), short facts share a large fraction of common words, producing false merges: e.g. 'The user works as a backend developer' vs 'The user works as a frontend manager' have very high word overlap and would overwrite each other despite being different facts (or facts about different jobs over time). Conversely, semantically identical facts phrased differently ('The user lives in Pune' vs 'User currently resides in Pune, Maharashtra') can fall under 0.6 and be stored as duplicates. It is also order/stopword sensitive and ignores negation ('likes' vs 'does not like' overlap heavily). The match is also restricted to identical category, so a fact recategorized by the LLM (work vs personal) never dedups.
- **Fix:** Given ChromaDB is already in the stack, dedup facts by embedding similarity instead of word overlap, or at minimum strip the common 'The user' prefix and stopwords before computing overlap and raise the threshold. Better: have the extraction LLM step receive the existing facts and emit add/update/delete operations explicitly, rather than reconstructing dedup heuristically.

### 31. [MEDIUM] Massive overlap: SQLite user_knowledge facts are NOT in ChromaDB, and ChromaDB stores raw transcript not facts — two stores that don't reinforce each other

- **Location:** `291-330, 492-512, 696-715`
- **Problem:** The two memory systems are disjoint and each has a gap. user_knowledge (curated facts) is injected wholesale into every prompt (get_user_knowledge, line 697) with no relevance filtering and no size cap (schema comment line 72 says 'No per-user limit') — on a -c 1024 context llama-server (llama-fast.service) this profile block can crowd out or overflow the 1024-token window as facts accumulate. Meanwhile ChromaDB stores every raw message but the high-value curated facts are never embedded, so RAG can't retrieve them and the profile injection can't be made selective. The result is redundancy (raw user messages that became facts are stored in both the transcript vectors and as facts) without complementarity. Note max_context_messages=100 (jarvis.json) further means get_recent_context can pull up to 100 messages into a 1024-token model — guaranteed truncation/overflow as a session grows.
- **Fix:** Pick one source of truth: either (a) embed curated facts into a separate Chroma collection and retrieve top-k relevant facts per query instead of dumping all facts, or (b) cap the injected profile (top N most-recently-updated or by relevance) to fit the 1024-token budget. Reconcile max_context_messages (100) with -c 1024; 100 messages cannot fit. Strongly consider raising llama -c (e.g. 2048/4096) or lowering max_context_messages to ~8-12.

### 32. [MEDIUM] store_message embeds even when chat_sessions row is missing — user_id defaults to 1, cross-user RAG contamination

- **Location:** `502-509`
- **Problem:** When session_id is 'default' (used by the voice listener run_listener.sh, which posts no session_id so QueryRequest.session_id defaults to 'default'), there is no row in chat_sessions, so user_id_row is None and user_id falls back to 1 (line 503). All 'default'-session messages from any authenticated principal are therefore embedded with user_id=1 and become retrievable by user 1's RAG. In the multi-user mode that auth supports, this mixes users' memories into the admin/user-1 vector space. The voice bridge authenticates with the MASTER_API_KEY which forces user_id=1 anyway, but any non-admin using the default session also lands in user_id=1's memory.
- **Fix:** Derive user_id for the vector from the authenticated request, not from the chat_sessions lookup; pass it into store_message. For the 'default' session, scope it per-user or disallow it in multi-user mode. At minimum, don't silently default to user 1.

### 33. [MEDIUM] Idle fact-extraction prompt is weak for a 2B /no_think model: no current-facts context, brittle JSON parsing, batch mixes unrelated turns

- **Location:** `227-248, 411-444`
- **Problem:** Several issues: (1) The prompt asks a 2B Qwen (reasoning off, /no_think system prompt) to emit a strict JSON array with no schema enforcement; parsing is json.loads after a naive ``` strip (lines 426-429). A 2B model frequently emits prose, trailing commas, or commentary, which silently yields zero facts (caught as JSONDecodeError, line 449) — extraction will often no-op. (2) The model is never shown the user's existing facts, so it can't honor the 'if the user corrects previous info, extract the CORRECTED version' rule across batches, and dedup is left entirely to the fragile word-overlap heuristic. (3) _extract_facts_batch concatenates up to 20 user messages from potentially different sessions/topics with 'User said:' lines (line 413), giving the model no turn boundaries or assistant context, which encourages hallucinated or merged facts — directly conflicting with the prompt's 'do NOT infer' rule. (4) n_predict=512 against a -c 1024 server leaves little room once the prompt + 20 messages are included; the input itself can approach the context limit and truncate.
- **Fix:** Feed existing facts into the prompt and ask the model to return explicit add/update/delete ops keyed by fact id. Process per-session (and ideally per recent exchange) rather than a 20-message cross-session blob. Add a JSON-repair fallback (extract first [...] substring) before giving up. Lower the batch size or raise llama -c so prompt+facts+output fit. Remove '/no_think' interference by using a dedicated extraction system prompt that does not inherit the global one (it already does via separate FACT_EXTRACTION_PROMPT, good — but verify the server isn't prepending the global prompt).

### 34. [LOW] RAG msg_id lookup uses list.index(doc) — wrong index on duplicate documents

- **Location:** `650-654`
- **Problem:** Inside the zip loop, to find the message ID for the current doc the code does idx = results['documents'][0].index(doc) and then reads results['ids'][0][idx]. list.index returns the FIRST matching position, so if two retrieved documents have identical text (very common: 'ok', 'thanks', 'yes', repeated questions), every duplicate maps to the first occurrence's ID. This corrupts the recent_context_ids dedup check (line 654) — a past duplicate may be wrongly skipped or wrongly kept. The loop is already iterating positionally via zip; the index is the loop position.
- **Fix:** Use enumerate over the zipped sequences and index ids[0] by the loop counter, e.g. for i,(doc,meta,dist) in enumerate(zip(...)): msg_id = results['ids'][0][i] if results.get('ids') else None. Note ChromaDB returns ids by default; the include list need not contain 'ids'.

### 35. [LOW] ChromaDB cleanup on delete is non-transactional with SQLite — vectors orphaned if delete throws

- **Location:** `547-566, 968-999`
- **Problem:** delete_session and admin_delete_user commit the SQLite deletes first, then delete Chroma vectors in a try/except that only logs on failure (lines 560-565, 985-993). If memory_collection.delete raises (or the process dies between the SQLite commit and the Chroma delete), the SQLite rows are gone but the vectors remain in ChromaDB forever, with no reconciliation path. Those orphaned vectors still carry user_id metadata and will be returned by retrieve_long_term_memory for that user_id even though the underlying messages/user no longer exist — a privacy/leak concern across re-created user IDs and a slow accumulation of dead vectors. There is no periodic reconciliation between the two stores.
- **Fix:** Best-effort is acceptable for a self-hosted box, but add a periodic reconciliation pass (in the existing memory worker) that lists Chroma IDs and deletes any not present in conversation_history. For user deletion specifically, prefer deleting by metadata filter (memory_collection.delete(where={'user_id': user_id})) so it doesn't depend on having every msg_id, which also fixes the case where a session was created before the chat_sessions.user_id ALTER and msg collection missed rows.

### 36. [LOW] RAG retrieves both user and jarvis transcript lines, injecting low-value assistant chatter as 'memories'

- **Location:** `626-665`
- **Problem:** The query has no speaker filter, so jarvis replies are embedded and retrieved as 'memories' and rendered 'Jarvis (past): ...' (line 663). Assistant text tends to be verbose, model-generated, and semantically close to the user's question, so it often dominates the top-k by similarity, pushing out the actual user facts. This wastes the tiny 1024-token budget on the model recalling its own prior phrasing rather than user information.
- **Fix:** Either restrict RAG retrieval to speaker='user' via where={'$and':[{'user_id':uid},{'speaker':'user'}]}, or weight/limit assistant results. Embedding only user messages (see performance finding) would solve this and the cost issue together.

### 37. [LOW] Schema/code drift: chat_sessions.user_id and conversation_history.facts_extracted exist only via ALTER in init_db, not in schema.sql

- **Location:** `4-16`
- **Problem:** schema.sql defines chat_sessions WITHOUT user_id and conversation_history WITHOUT facts_extracted; both are added at runtime by best-effort ALTER TABLE in init_db (main.py lines 188, 195) inside bare try/except. A fresh DB created from schema.sql alone (e.g. by tooling, tests, or manual psql-style restore) lacks these columns until the app runs init_db. The memory worker's _get_unprocessed_messages swallows the missing-column case and returns [] (lines 373-375), so on a half-initialized DB fact extraction silently never runs with no error surfaced. The legacy semantic_facts table (schema.sql lines 40-45) and FTS5 conversation_fts (lines 19-38) appear unused by the memory/RAG code reviewed — dead weight whose DELETE triggers still fire on every conversation delete.
- **Fix:** Fold the ALTER-added columns directly into schema.sql so the schema is self-describing. Remove or wire up the unused semantic_facts table and FTS5 search if not used. Make _get_unprocessed_messages log (not silently swallow) a missing-column error so a broken init is visible.


## Architecture / Code Quality (19)

### 38. [HIGH] 1134-line single-file orchestrator mixes every concern (no layering)

- **Location:** `1-1135`
- **Problem:** main.py is a single module that contains config loading, logging setup, the auth/security middleware, password hashing, the SQLite data-access layer (sessions, history, knowledge CRUD), the ChromaDB vector store, a background fact-extraction worker, the LLM HTTP client (blocking and streaming), prompt assembly/RAG, Piper TTS subprocess invocation, ~12 chat/admin/knowledge HTTP endpoints, Pydantic models, and static-file mounting. There are no packages or modules: no auth.py, db.py, llm.py, memory.py, schemas.py, routers/. Module-level side effects (CONFIG load at import, ChromaDB client + SentenceTransformer model instantiated at import on lines 205-212) mean importing the module starts loading a 300m embedding model. A reviewer would immediately flag this as the central design problem: nothing can be reasoned about, swapped, or tested in isolation.
- **Fix:** Split into a package: config.py (typed settings), db.py (connection/repository layer), auth.py (middleware + hashing + token verification), llm.py (client), memory.py (RAG + worker), schemas.py (Pydantic), and FastAPI routers (chat, admin, knowledge, auth). Defer ChromaDB/embedding-model init into a startup/lifespan handler rather than at import time.

### 39. [MEDIUM] Dead dual-brain config: reasoning_brain_url and active_model are never read

- **Location:** `5-6`
- **Problem:** config/jarvis.json declares llm.reasoning_brain_url (http://127.0.0.1:8080/...) and llm.active_model = "fast", implying a routed dual-model architecture. main.py only ever reads llm.fast_brain_url (line 56: LLM_URL = CONFIG["llm"]["fast_brain_url"]). grep confirms reasoning_brain_url and active_model appear nowhere in the Python code. There is no second llama-server service (only llama-fast.service on 8081); nothing listens on 8080. This is vestigial config advertising a capability that does not exist, which misleads any reader about the system's actual topology.
- **Fix:** Either remove the dead keys, or actually implement model selection (read active_model, map to the corresponding URL). Do not ship config that describes a non-existent component.

### 40. [MEDIUM] Declared-but-unread config: max_context_tokens

- **Location:** `9`
- **Problem:** llm.max_context_tokens = 4096 is declared but never read anywhere in main.py (grep returns no match). Worse, it is factually wrong and dangerous as documentation: llama-fast.service runs with -c 1024 (1024-token context window), yet build_messages() (lines 691-720) concatenates the system prompt, the full user-knowledge profile, up to RAG_MAX_RESULTS recalled memories, AND get_recent_context with MAX_CONTEXT_MESSAGES = 100 messages (config memory.max_context_messages, line 20). On a 1024-token server this guarantees silent context truncation/overflow. The config value neither bounds the prompt nor matches the server. max_input_length=500 (line 14) is also duplicated by the hardcoded REGULAR_MAX_INPUT=500 constant (main.py:723) and is not the value actually enforced.
- **Fix:** Remove max_context_tokens or wire it into a real token-budgeting step in build_messages that trims context to fit the server's -c value. Reconcile max_context_messages=100 with the 1024-token llama-server window. Replace the hardcoded REGULAR_MAX_INPUT with the config max_input_length.

### 41. [MEDIUM] Duplicate, divergent frontend: app.js (vanilla) and App.jsx (React) are two full chat clients

- **Location:** `1-348`
- **Problem:** There are two complete, independently-maintained chat-UI implementations. App.jsx (578 lines, React 19) drives /chat/stream with SSE streaming and is the served '/' app. static/app.js (348 lines, vanilla JS) is a second full client (login, sessions, history, advanced params, send) that posts to the NON-streaming /inbox endpoint (app.js:274). They have drifted: the React app streams, the vanilla app does not. Maintaining two clients doubles the surface for bugs and is the kind of duplication a reviewer calls out instantly. It is unclear which is canonical; the static one appears to be legacy.
- **Fix:** Delete the unused vanilla client (or clearly scope it to the admin panel only) so there is a single source of truth for the user chat UI.

### 42. [MEDIUM] Pervasive bare except: and broad except Exception that swallow errors silently

- **Location:** `104,160,189-196,368-396,855,868,921,1024`
- **Problem:** Bare `except:` clauses appear throughout, several silently discarding failures: verify_password (line 104) returns False on ANY exception, hiding malformed-hash bugs; the api_keys usage-count update (line 160 `except: pass`); init_db migrations (lines 189,191,193,195 `except: pass`) which hide real schema errors as well as the expected duplicate-column case; the Piper TTS call (line 855 `except Exception: pass`); title generation (lines 868, 921 `except: pass`); admin_list_keys column fallback (line 1024 `except:`). _get_unprocessed_messages (line 373) and _mark_messages_processed (line 395) catch Exception and return/pass, so a query bug looks identical to 'no data'. Bare except also catches KeyboardInterrupt/SystemExit. This makes failures invisible in logs and is a textbook anti-pattern reviewers penalize.
- **Fix:** Catch specific exceptions (sqlite3.OperationalError for the migration/duplicate-column cases, urllib.error.URLError for HTTP, json.JSONDecodeError for parsing). Never use bare `except:`. Log at minimum when swallowing. For migrations, check column existence explicitly rather than try/except-as-control-flow.

### 43. [MEDIUM] Per-call SQLite open/close on every operation; no connection pooling or reuse

- **Location:** `91-96`
- **Problem:** get_db() (lines 91-96) opens a brand-new sqlite3.Connection, sets row_factory, and runs `PRAGMA journal_mode=WAL` and `PRAGMA foreign_keys=ON` on EVERY single call, then every helper closes it in a finally. A single /inbox request opens and tears down the DB many times (session ownership check, get_recent_context, get_user_knowledge, _get_recent_message_ids, two store_message inserts, rename). journal_mode=WAL is a persistent database property, so re-issuing it per connection is wasted work. There is no connection caching, no FastAPI dependency-injected connection, no request-scoped transaction. This pattern also means store_message's two inserts and the vector-DB add are not in one transaction. On a 2-core Sandy Bridge box this churn is measurable overhead.
- **Fix:** Use a request-scoped connection via a FastAPI dependency (yield), or a small connection pool / thread-local. Set WAL and foreign_keys once at startup. Wrap multi-statement operations (e.g. store_message's two inserts, delete_session) in a single transaction.

### 44. [MEDIUM] Zero automated tests and no test/lint tooling

- **Location:** `1-13`
- **Problem:** There are no application tests anywhere (the only ./whisper/tests are vendored third-party whisper.cpp code). pyproject.toml lists only runtime deps (chromadb, fastapi, sentence-transformers, uvicorn) — no pytest, no httpx test client, no ruff/black/mypy, no [tool.*] config, no dev-dependencies group, no CI. Critical, easily-unit-testable logic ships untested: verify_password/hash_password round-trip, the >60% word-overlap dedup heuristic in store_fact (lines 301-318), the JSON/markdown-fence parsing in _extract_facts_batch (lines 426-429), the RAG distance filtering, check_rate_limit windowing. The monolithic structure (Finding 1) is itself what makes these hard to test, since everything is bound to module-level singletons and side effects.
- **Fix:** Add pytest + a FastAPI TestClient (httpx), and ruff/mypy as dev dependencies with a CI workflow. Refactor pure logic (password hashing, dedup heuristic, fact-JSON parsing, rate limiter) out of the request path so it can be unit-tested without a live DB/LLM.

### 45. [MEDIUM] Orphan schema objects: semantic_facts table and FTS5 search infrastructure are never used

- **Location:** `19-45`
- **Problem:** schema.sql defines a full FTS5 virtual table conversation_fts plus three sync triggers (ai/ad/au, lines 19-38) and a semantic_facts table (lines 40-45). grep confirms none of conversation_fts, semantic_facts are referenced anywhere in main.py. Full-text search was superseded by ChromaDB vector RAG, but the FTS5 table and its three triggers are still created and, crucially, still fire on every INSERT/UPDATE/DELETE to conversation_history — pure write overhead with no reader. The file header even advertises 'Utilizing FTS5 ... without external dependencies', which is now false given the ChromaDB+sentence-transformers dependency. This is dead schema that misrepresents the architecture and slows every message write.
- **Fix:** Drop conversation_fts, its triggers, and semantic_facts if vector RAG is the chosen approach; or remove ChromaDB and use FTS5. Do not maintain both. Update the misleading header comment.

### 46. [MEDIUM] Schema vs. runtime drift: core columns added via try/except ALTER migrations instead of being in schema.sql

- **Location:** `188-196`
- **Problem:** Columns essential to current behavior are absent from schema.sql and bolted on at runtime in init_db() via ALTER TABLE wrapped in `except: pass`: chat_sessions.user_id (which the ENTIRE multi-user ownership model depends on), api_keys.usage_count, api_keys.last_used_at, and conversation_history.facts_extracted (which the memory worker filters on). So the canonical schema file does not describe the real schema; the true schema is the union of schema.sql plus imperative migrations plus REFERENCES clauses defined in code strings. admin_list_keys even has a runtime fallback query (lines 1022-1025) for when these columns 'don't exist yet'. This ad-hoc migration-by-exception approach is fragile and a clear maintainability smell.
- **Fix:** Put all columns in schema.sql (it already uses IF NOT EXISTS / CREATE patterns) or adopt a real migration tool/versioned migration files. Remove the column-existence fallback query once the schema is authoritative.

### 47. [MEDIUM] Blocking I/O (urllib, subprocess, sqlite) inside async/sync FastAPI handlers with workers=1

- **Location:** `569-617,803-870,872-928`
- **Problem:** request_llm and request_llm_stream use blocking urllib.request.urlopen (lines 583, 603) with a 120s timeout. /inbox and /chat/stream are declared as plain `def` endpoints, so FastAPI runs them in the threadpool, but the LLM call (~3.7 tok/s on this CPU, so many seconds per response), the synchronous DB calls, and a blocking subprocess.run to Piper (line 849) all execute inline. jarvis-orchestrator.service runs uvicorn with --workers 1 and llama-server with --parallel 1, so concurrency is effectively serialized; the default threadpool (40 threads) can also oversubscribe the 1-parallel llama backend. The streaming generator holds the urllib connection open for the whole generation. The security_middleware (async) does synchronous sqlite work (lines 139-162) directly in the event loop, blocking it for every authenticated request. For a 'multi-user auth exists' service this concurrency model will not scale beyond near-single-user.
- **Fix:** Move blocking HTTP to an async client (httpx.AsyncClient) or run via run_in_threadpool deliberately; do not perform synchronous sqlite in async middleware (offload or make the middleware a sync dependency). Bound concurrency to the llama --parallel value. Document the single-user concurrency assumption if intentional.

### 48. [MEDIUM] Inconsistent auth bypass for session_id 'default' allows cross-user data access

- **Location:** `785-797,814-820`
- **Problem:** Ownership checks are special-cased to skip when session_id == 'default' (history line 788, inbox line 814, chat_stream line 882). The literal 'default' is a shared, un-owned session bucket usable by every authenticated user: get_recent_context/store_message operate on it with no user scoping, so any user reading or writing session 'default' shares one conversation stream, and messages stored there get user_id=1 in the vector metadata (store_message line 503 defaults to 1). Beyond the security angle, this is a design smell: a magic string sentinel ('default') threaded through six call sites with bespoke conditionals instead of a proper per-user default session.
- **Fix:** Create a real per-user default session on first use instead of a global 'default' sentinel, and apply the same ownership check uniformly. Remove the scattered `if session_id != 'default'` special cases.

### 49. [LOW] request_llm/request_llm_stream share a 9-parameter signature duplicated verbatim

- **Location:** `569-617`
- **Problem:** request_llm and request_llm_stream have identical 9-sampling-parameter signatures and identical 18-line bodies for building the `data` dict (the only difference is stream=False/True and response handling). The same parameter list is threaded again through /inbox (lines 827-831) and /chat/stream (lines 897-901) and again on the QueryRequest model (lines 729-738). Adding one sampling param requires editing four places. This is copy-paste that a reviewer would ask to collapse.
- **Fix:** Extract a single _build_llm_payload(messages, params, stream) helper (or pass a dataclass/dict of sampling params) shared by both functions, and pass the params object through the endpoints rather than enumerating each field.

### 50. [LOW] Naive fact-dedup heuristic and per-fact full-table scan in store_fact

- **Location:** `291-330`
- **Problem:** store_fact deduplicates with a word-set Jaccard-style overlap > 0.6 (lines 306-310). 'The user lives in Pune' vs 'The user lives in Delhi' share most words and would be treated as the same fact, silently overwriting a correct fact with a different one; conversely small rewordings under threshold create duplicates. It also re-fetches all existing facts in the category and loops in Python on every insert. Given a dedicated ChromaDB vector store already exists for semantic similarity, doing string-overlap dedup in SQL is both inconsistent with the rest of the design and low-quality.
- **Fix:** Use the vector store (cosine similarity) for semantic dedup, or at minimum normalize/compare on a stable key. Document the heuristic's known failure modes if kept. Avoid the per-insert full-category scan.

### 51. [LOW] CORS allow_origins=['*'] combined with Bearer-token auth and an admin surface

- **Location:** `74-79`
- **Problem:** CORS is configured with allow_origins=['*'] and allows POST/GET/PUT/DELETE plus Authorization header (lines 76-78), while the app exposes authenticated admin endpoints (/admin/users, /admin/api_keys). Wildcard CORS on a service that also binds 0.0.0.0:5000 (jarvis-orchestrator.service line 12-13) means any website a logged-in user visits can call the API with tokens it can read if they were exposed; more importantly it signals no thought about origin restriction. The master api_key is also stored in plaintext in jarvis.json (line 2) and read by run_listener.sh via a python one-liner — committed-secret risk. A reviewer flags wildcard CORS on an authenticated, internet-bindable admin API.
- **Fix:** Restrict allow_origins to the known frontend origin(s). Bind to 127.0.0.1 unless remote access is required (then put behind TLS/reverse proxy). Move the master key out of a world-readable JSON file (env var / secrets store) and rotate it.

### 52. [LOW] Global mutable state (rate limiter, activity clock, worker flag) unsuited to multi-process and untestable

- **Location:** `81-89,224-225,250-253`
- **Problem:** Rate limiting uses an in-process defaultdict _rate_store (lines 81-89); idle tracking uses module-global _last_activity_time (lines 224, 250-253); the worker uses _memory_worker_running (line 225). These are process-local: they break under more than one uvicorn worker (the service currently pins --workers 1, which is itself a scalability constraint masking the design flaw) and grow unbounded since _rate_store keys (client IPs) are never evicted. They also force tests to manipulate module globals. The rate limiter is also bypassed entirely for admins and for the master key path (line 167, 134-137).
- **Fix:** Externalize rate-limit and activity state (e.g. SQLite/Redis) or encapsulate in an injectable object; evict stale IP buckets. Avoid module-global mutable state so logic is testable and multi-worker-safe.

### 53. [LOW] Fragile LLM response parsing assumes fixed shape; KeyError surfaces as 500

- **Location:** `423,834,864,917`
- **Problem:** Responses from the LLM are indexed positionally without validation: result['choices'][0]['message']['content'] in _extract_facts_batch (line 423), /inbox (line 834), and title generation (lines 864, 917). request_llm raises HTTPException(503) on transport errors but returns whatever JSON the backend sent on success; if llama-server returns an error object or an empty choices list, these unchecked indexes raise KeyError/IndexError that escape as an unhandled 500 (or, in the worker, get swallowed by the broad except at line 451). No schema/shape check, no defensive .get with fallback.
- **Fix:** Validate the response shape (check 'choices' is non-empty) and raise a clear, typed error or return a graceful fallback. Centralize this in request_llm so all callers get consistent handling.

### 54. [LOW] Deprecated FastAPI startup hook and import-time heavy initialization

- **Location:** `1127-1131,201-212`
- **Problem:** @app.on_event('startup') (line 1127) is deprecated in modern FastAPI in favor of lifespan handlers. Meanwhile genuinely heavy initialization that SHOULD be in startup is instead done at import time: the SentenceTransformer embedding model and ChromaDB PersistentClient are created at module import (lines 205-212), and CONFIG is loaded at import (line 52). The import statement `from chromadb.utils import embedding_functions` is buried at line 201 mid-file rather than at the top with other imports, and `import hmac` is inside the middleware function (line 133). These import/initialization placements make the module's startup cost and dependencies non-obvious and complicate testing (importing the module loads a 300m model).
- **Fix:** Use a lifespan context manager; move embedding-model/ChromaDB construction into it so import is cheap and testable. Hoist all imports to the top of the file.

### 55. [LOW] Frontend speed metric and token math are fabricated approximations presented as measurements

- **Location:** `255-258`
- **Problem:** On stream completion App.jsx computes tok/s as answer.split(/\s+/).length * 1.3 / wallTime (lines 256-258) — a word-count-times-1.3 guess divided by total wall time including network/first-token latency, displayed to the user as 'X tok/s'. The backend already returns a real predicted_per_second from llama timings (main.py:838-839) for the non-streaming path, but the streaming path discards real timings and the UI invents a number. Presenting a fabricated metric as a diagnostic is a quality issue a reviewer notices.
- **Fix:** Emit real token/timing data in the stream 'done' event from the backend and display that, or label the client estimate clearly as approximate.

### 56. [LOW] App.jsx is a 578-line single component with no decomposition and stale-closure bug

- **Location:** `1-578`
- **Problem:** The entire client is one App() component holding ~25 useState hooks (auth, sessions, messages, 9 sampling params, UI toggles) with no extraction of Login, Sidebar, MessageList, ParamPanel components, no custom hooks, no state reducer, and a hand-rolled markdown renderer (lines 293-311). loadHistory (lines 124-140) reads `sessions` from a closure to resolve the title (line 134) but is called right after createSession before the sessions state updates, so the title can resolve stale/empty — a classic stale-closure smell from cramming everything into one component. The mounted Vite build dir is also required at runtime by the server, coupling deploy steps.
- **Fix:** Decompose into components and custom hooks (useAuth, useChatStream, useSessions); consider a reducer for message state; use a vetted markdown library instead of the regex renderer.


## Frontend (13)

### 57. [HIGH] Auth token stored in localStorage — readable by any XSS, never expires client-side, 30-day server token

- **Location:** `7, 91-92, 116`
- **Problem:** The session token is kept in localStorage (App.jsx 7, 91; app.js 72-73; admin.html 261). localStorage is accessible to any JavaScript on the origin, so the stored-XSS issues above (and the LLM-output rendering path) can exfiltrate the token directly. The token is a 30-day bearer token (main.py 759) sent as Authorization: Bearer on every request; there is no httpOnly cookie option and no CSRF-independent protection. Combined with CORS allow_origins=['*'] (main.py 76) the bearer model is broad. For a self-hosted single-user box the risk is reduced, but multi-user auth exists and the admin panel raises the stakes.
- **Fix:** Prefer an httpOnly, Secure, SameSite cookie issued by /auth/login so JS cannot read the token. If localStorage must stay, treat eliminating XSS (admin.html, renderMessageContent) as mandatory, shorten token lifetime, and add a server-side logout/revocation for auth_sessions (currently doLogout only deletes the local copy; the row in auth_sessions is never invalidated).

### 58. [MEDIUM] Voice output toggle in React UI is silently dead — /chat/stream never reads voice_feedback

- **Location:** `212, 218, 437`
- **Problem:** App.jsx sends voice_feedback: voice in the payload (line 212) to POST /chat/stream (line 218), and exposes a 'Voice Output' checkbox (line 437). But the backend /chat/stream handler (main.py event_generator, lines 894-928) never references request.voice_feedback and never invokes Piper — only /inbox (main.py 843-854) synthesizes audio and returns an 'audio' base64 field. The streaming endpoint also yields no audio field. So toggling Voice Output in the React UI does literally nothing: no speech is ever produced and no error is shown. The user is misled into thinking voice works. (Note: the legacy vanilla app.js posts to /inbox and DOES play r.audio at line 279, so the feature only works in the old UI.)
- **Fix:** Either (a) make /chat/stream honor voice_feedback by synthesizing the full answer with Piper after streaming completes and emitting a final SSE frame like data: {"audio": "<b64>"} (which App.jsx must then decode and play), or (b) remove the Voice Output checkbox from App.jsx until the streaming path supports it. Do not ship a control that has no effect.

### 59. [MEDIUM] Server-side session/token is never revoked on logout

- **Location:** `104-111`
- **Problem:** doLogout() (App.jsx 104-111) and app.js clearAuth()/logout (app.js 74, 329) only remove the token from localStorage. There is no DELETE call to the server and no endpoint exists to invalidate the auth_sessions row, so the token remains valid for the full 30 days (main.py 759-760). Anyone who captured the token (via the XSS paths above, a shared machine, logs, etc.) keeps access after the user 'logs out'.
- **Fix:** Add a POST /auth/logout endpoint that deletes the auth_sessions row for the presented token, and call it from both UIs before clearing localStorage.

### 60. [MEDIUM] Streaming errors are swallowed — no user-visible error, broken UI state on failure

- **Location:** `270, 274-276, 906`
- **Problem:** In send(), every per-line JSON parse is wrapped in try/catch with an empty body (line 270), and the outer catch only console.error(e) (lines 274-276) with no toast/banner. If /chat/stream fails mid-stream, the backend emits data: {"error": "AI backend error"} (main.py 906), which App.jsx ignores entirely (it only looks at data.content and data.done). The optimistic empty 'jarvis' bubble pushed at line 203 is left in place showing either nothing or a partial answer, with no error shown to the user. Also request_llm_stream yields the literal string '<ERROR: AI backend error>' (main.py 617) which would be rendered as message text. Contrast app.js, which has a toast() and shows err.message (app.js 280-283). The React UI has no toast mechanism at all.
- **Fix:** Handle data.error frames in the stream loop and surface them (toast/inline error). On the outer catch, replace the dangling streaming bubble with an error state. Add a lightweight toast component to App.jsx.

### 61. [MEDIUM] Accessibility: interactive controls are non-semantic, missing labels, ARIA, and keyboard support

- **Location:** `338, 376-378, 435-441, 453-457, 527-532`
- **Problem:** Multiple a11y gaps in App.jsx: the sidebar-overlay div has onClick but no role/keyboard handler (line 338); icon-only buttons (☰ toggle line 471, send ▶/■ line 567, [R]/[D] history buttons 456-457, Copy 532) have no aria-label, so screen readers announce nothing meaningful; the history item is a clickable <div> not a <button> (line 453); the Voice/checkbox and sliders use <label> elements that are not associated via htmlFor/id (e.g. 390-392, 436-437), so clicking the label or SR focus does not target the input; status is conveyed only by color dots (status-dot online/offline, lines 361-362, 477) with no aria-live; streaming updates and the typing indicator are not announced (no aria-live region on messages). Login inputs (327-328) have placeholders but no associated <label>.
- **Fix:** Use <button> for clickable items, add aria-label to icon-only buttons, associate <label htmlFor> with input ids, add role/keyboard handlers (or aria-hidden) to the overlay, add an aria-live='polite' region around the message list/typing indicator, and pair status dots with text already present via aria so state isn't color-only.

### 62. [LOW] Token-estimate 'tok/s' is fabricated client-side and misleading

- **Location:** `255-258`
- **Problem:** App.jsx computes speed as words*1.3 / wallTime (lines 256-258). This includes network/queueing time and a crude word heuristic, so on this ~3.7 tok/s CPU box it will routinely misreport throughput. The backend already computes an accurate predicted_per_second from llama-server timings for /inbox (main.py 837-841) but /chat/stream does not forward it. The displayed number is essentially noise.
- **Fix:** Have /chat/stream include the real timings (e.g. in the final done frame) and display that, or drop the tok/s readout for the streaming path.

### 63. [LOW] [DONE] sentinel only breaks the inner loop, not the read loop

- **Location:** `243`
- **Problem:** if (dataStr === '[DONE]') break (line 243) is inside the for (const line of lines) loop, so it only stops processing remaining lines in the current chunk, not the outer while(true) reader loop. In practice the backend /chat/stream never emits 'data: [DONE]' (it emits data: {"done": true}, main.py 924-926), so this branch is dead code and the loop relies on reader done. It is a latent bug if the upstream SSE format ever changes, and signals the two code paths drifted (request_llm_stream strips upstream [DONE] at main.py 606).
- **Fix:** Drive completion off the JSON {done:true} frame and reader done; remove or fix the dead [DONE] handling (use a labeled break if you intend to honor it).

### 64. [LOW] Streamed/raw LLM output rendered through hand-rolled markdown — partial-token and edge-case rendering risk

- **Location:** `293-311`
- **Problem:** Good news: renderMessageContent (App.jsx 293-311) builds React elements and inserts text as JSX children (auto-escaped) and code via <code>{...}</code> — there is NO dangerouslySetInnerHTML, so LLM output cannot inject HTML/script. app.js renderContent is likewise textContent-based and safe. So there is no XSS from LLM output in either chat UI (only admin.html is vulnerable). The residual issue is correctness, not security: the regex splitter runs on every partial streaming update, so unterminated ``` fences or backticks flicker as raw markers until the closing fence arrives, and nested/edge markdown is mis-parsed. Worth noting since the prompt explicitly asked about this risk.
- **Fix:** No security change needed for the chat path. If desired, use a tested markdown renderer (e.g. marked + DOMPurify, or react-markdown) instead of the bespoke parser to fix streaming flicker and edge cases. Keep using escaped React children — never switch to dangerouslySetInnerHTML.

### 65. [LOW] loadHistory title resolution races against stale sessions state

- **Location:** `124-140, 48-54`
- **Problem:** loadHistory(sid) looks up the title from the sessions state closure (line 134). On initial mount the effect calls loadSessions() and loadHistory('default') in parallel (lines 50-52); loadHistory closes over the empty sessions array, so for a non-default sid the title can resolve to undefined until a later render. More generally, several handlers call loadSessions() fire-and-forget after mutations (send line 278, createSession 149) and depend on ordering; currentTitle and sessions can briefly disagree. Functional but fragile.
- **Fix:** Derive the current title from currentSessionId + sessions via a memo rather than storing a separate currentTitle, or pass the title explicitly into loadHistory instead of reading it from closure state.

### 66. [LOW] send() pushes optimistic messages with two sequential setState calls and rebuilds full array per token

- **Location:** `202-203, 248-252`
- **Problem:** Two separate setMessages calls (lines 202-203) append the user and placeholder messages in sequence; harmless with the functional updater but unnecessary. More notably, each streamed token does setMessages(prev => { const newMsgs=[...prev]; newMsgs[len-1]=...; }) (248-252), cloning the entire messages array and re-rendering every message on every token. On long conversations on this 2-core CPU box that is wasteful (the bottleneck is the model, but the UI still re-renders all bubbles each chunk).
- **Fix:** Combine the two appends into one setState, and consider tracking only the streaming buffer in a ref/local state and committing to messages on done, or memoize message rows so non-changing bubbles don't re-render.

### 67. [LOW] React UI assumes single 'default' session and silently creates sessions; mismatch with app.js boot behavior

- **Location:** `48-54, 124, 185-196`
- **Problem:** App.jsx initializes currentSessionId='default' and calls loadHistory('default') (line 52). The backend treats 'default' specially (no ownership check, never titled — main.py 788, 814), and App.jsx auto-creates a real session on first send (185-196). app.js instead boots into the user's most recent real session (app.js 318-320). The two front-ends therefore present different initial state for the same backend, and the 'default' pseudo-session in React can accumulate orphan behavior (history for 'default' is shared/unsowned). Minor, but the inconsistency is a maintenance hazard given two UIs target the same API with different endpoints (/chat/stream vs /inbox).
- **Fix:** Pick one front-end as canonical. If keeping App.jsx, load the latest real session on mount like app.js does, and avoid relying on the unowned 'default' session.

### 68. [LOW] nPredict / seed numeric inputs can become NaN and be sent to the API

- **Location:** `429, 433, 211`
- **Problem:** Max Tok (line 429) and Seed (line 433) use parseInt(e.target.value) with no fallback; clearing the field yields NaN, which is stored in state and serialized — JSON.stringify(NaN) becomes null, so n_predict/seed silently arrive as null (acceptable here since the model fields are Optional), but the on-screen value also shows NaN and the control becomes confusing. app.js has the same parseInt(...) without guards (app.js 269-270).
- **Fix:** Guard with Number.isNaN fallbacks (e.g. const v = parseInt(...); setNPredict(Number.isNaN(v)?1024:v)) and clamp to sane ranges.

### 69. [LOW] App.jsx system_prompt override is unbounded and trusts client; admin-only intent not enforced in UI

- **Location:** `213, 440-441`
- **Problem:** The System Prompt Override textarea (lines 440-441) sends system_prompt for any logged-in user (line 213). The backend accepts it for every user in build_messages (main.py 691-692, 824) with no admin gate and no length cap on the prompt itself (only text is length-limited). On a 1024-token context model (-c 1024, llama-fast.service) a long override will silently crowd out / truncate the actual conversation, producing degraded answers with no warning. This is a UX/contract gap rather than a security hole, but the UI exposes a footgun.
- **Fix:** Either restrict the override to admins in both UI and backend, or cap its length and warn the user about the 1024-token budget; surface truncation.


## Ops / Performance / Deploy (12)

### 70. [HIGH] Idle memory worker runs 512-token LLM extraction that contends with foreground requests for the only LLM slot and 2 CPU cores

- **Location:** `457-489 (_memory_worker), 400-455 (_extract_facts_batch), 422 (request_llm n_predict=512)`
- **Problem:** A background daemon thread wakes every 30s; after 120s of inactivity it pulls up to 20 unprocessed user messages and calls request_llm with n_predict=512 per user-group. The idle check uses `_last_activity_time`, updated only at the START of /inbox and /chat/stream via _update_activity() (lines 805, 874). Two concrete problems on this 2C/4T box: (a) A streaming generation can easily run longer than 120s (3.7 tok/s, up to ~512 new tokens plus huge prompts). _update_activity fires once at request start, so 120s later the worker can declare the system 'idle' WHILE a generation is still streaming, fire its own 512-token extraction, and now TWO generations compete for llama-server's single --parallel 1 slot and the 2 physical cores. With -t 2 each, that is CPU oversubscription and both requests slow to a crawl. (b) Extraction itself is heavy (512 tokens at 3.7 tok/s = ~2+ minutes) and holds the LLM slot, so a user who returns mid-extraction waits behind it.
- **Fix:** Update activity at request COMPLETION (and during streaming) not just at start; gate the worker behind the same LLM semaphore so extraction never overlaps a user generation; lower extraction n_predict (256), reduce batch size, and consider nice/ionice or a cooldown after the foreground request finishes.

### 71. [HIGH] .gitignore omits models/, piper/, whisper/, memory/ and logs/ - committing would add ~9.7GB including a model binary, the SQLite DB with password hashes, and chroma vectors

- **Location:** `1-10 (entire file)`
- **Problem:** The repo is currently un-committed (git ls-files returns 0). The .gitignore only excludes Python build artifacts and .venv. It does NOT exclude models/ (3.8G, includes Qwen3.5-2B-Q4_K_M.gguf), whisper/ (761M, includes ggml models and built binaries), piper/ (112M binary + onnx voices), memory/ (jarvis.db with PBKDF2 password hashes and auth tokens, plus chroma_db), or logs/. A `git add .` would stage ~9.7GB of binaries and runtime state. This bloats the repo permanently (git keeps blobs forever), makes clones/pushes huge, and leaks the user DB (auth_sessions tokens, api_keys, user_knowledge personal facts) and benchmark logs into version control.
- **Fix:** Add to .gitignore: models/, piper/, whisper/, memory/, logs/, frontend/dist/, and config/jarvis.json (see separate secret finding). Ship models/binaries via a download script or release artifacts, not git. Verify nothing large/secret is already staged before the first commit.

### 72. [HIGH] config/jarvis.json (containing the master API key) is not gitignored and will be committed

- **Location:** `2 (api_key)`
- **Problem:** jarvis.json holds `api_key` (line 2) which main.py:55/134 treats as a MASTER bypass token granting forced admin (user_id=1, is_admin=True) on every endpoint. The file is not excluded by .gitignore, so the first commit publishes a full-admin credential into git history. The schema/admin surface (admin user CRUD, api_key issuance) is fully reachable with this single static key over 0.0.0.0:5000.
- **Fix:** Gitignore config/jarvis.json, commit a jarvis.example.json with placeholders, and rotate the key. Load secrets from an environment variable or a non-tracked file with 0600 perms.

### 73. [HIGH] -c 1024 context is too small for the orchestrator's prompt-building strategy, causing silent truncation/eviction

- **Location:** `10 (-c 1024); main.py:62 MAX_CONTEXT_MESSAGES=100; config/jarvis.json:9 max_context_tokens=4096; config/jarvis.json:21 max_context_messages=100`
- **Problem:** llama-server is started with -c 1024 (total KV context = prompt + generated tokens). But the orchestrator builds prompts that can vastly exceed 1024 tokens: build_messages() injects the system prompt, the FULL user knowledge profile (get_user_knowledge, unbounded), up to RAG_MAX_RESULTS recalled memories, AND get_recent_context with MAX_CONTEXT_MESSAGES=100 prior messages (main.py:514-522, 691-720). config also advertises max_context_tokens=4096 (jarvis.json:9), which is inconsistent with the server's 1024. When the assembled prompt exceeds 1024, llama-server will either error or (with context shift) silently drop the oldest tokens - which here means the system prompt / user profile / instructions get evicted first, degrading answer quality and 'forgetting' the very memory the system worked to inject. The fact-extraction path also sends a large prompt (FACT_EXTRACTION_PROMPT + up to 20 user messages) plus n_predict=512, which alone can blow past 1024.
- **Fix:** Either raise -c to match real prompt sizes (e.g. 4096, accepting more RAM/KV and slower prompt eval) AND set MAX_CONTEXT_MESSAGES far lower, OR keep -c 1024 and hard-cap the assembled prompt (trim recent context to a few messages, cap knowledge/RAG injection) so it provably fits. Make config max_context_tokens agree with -c. Note: larger -c increases KV cache RAM and prompt-eval time on a no-AVX2 CPU.

### 74. [MEDIUM] Single uvicorn worker + blocking urllib LLM call serializes ALL requests behind one slow generation

- **Location:** `803-870 (sync def process_input), 581-587 (request_llm), service: systemd/jarvis-orchestrator.service:11-14 (--workers 1)`
- **Problem:** The hot-path endpoints /inbox and /chat/stream are defined as plain `def` (synchronous), and the LLM call uses blocking `urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)` with REQUEST_TIMEOUT=120s. FastAPI runs sync endpoints in a threadpool (default 40 threads via anyio), so requests are not literally serialized at the Python layer - BUT the real bottleneck is the model itself: llama-server is launched with `--parallel 1` (llama-fast.service:14), so the LLM backend processes exactly one generation at a time. At ~3.7 tok/s a 150-token reply takes ~40s, and a 512-token fact-extraction or a full answer can monopolize the single CPU-bound llama slot. Any second user request blocks waiting on llama-server for the duration of the first generation. The orchestrator's threadpool just means many requests queue politely; the user-perceived effect is identical to full serialization. With only 2 physical cores there is no spare CPU to hide this. So yes: a slow generation effectively blocks all other LLM-backed requests.
- **Fix:** Accept single-stream LLM as a hardware reality, but make it explicit and bounded: (1) add an application-level semaphore/queue so concurrent /inbox calls fail fast or queue with a visible 'busy' state rather than piling up threadpool threads each holding a DB connection; (2) make request_llm use httpx async + async endpoints so the event loop isn't consuming a threadpool slot while merely waiting on I/O; (3) document that --parallel 1 means strictly one generation at a time.

### 75. [MEDIUM] Orchestrator binds 0.0.0.0:5000 (network-exposed) while only llama-server is bound to localhost

- **Location:** `12 (--host 0.0.0.0); config/jarvis.json:12; llama-fast.service:12 (--host 127.0.0.1)`
- **Problem:** llama-server correctly binds 127.0.0.1 (llama-fast.service:12), but the orchestrator binds 0.0.0.0:5000, exposing the auth surface, admin panel (/admin), and master-key bypass to the whole LAN/network. Combined with CORS allow_origins=['*'] (main.py:76) and a static master key, any host that can reach port 5000 can attempt the admin API. For a 'single-user-ish self-hosted' box this is a broad exposure; there is no TLS and no reverse proxy in the units.
- **Fix:** Bind to 127.0.0.1 (or a Tailscale/WireGuard interface) and front with a reverse proxy that terminates TLS, or restrict with a firewall. Tighten CORS to the actual frontend origin.

### 76. [MEDIUM] No log rotation: FileHandler appends to a single orchestrator.log forever; systemd units have no journald identifier/limits

- **Location:** `34-41 (logging.FileHandler, no rotation); systemd/jarvis-orchestrator.service (no StandardOutput/SyslogIdentifier); systemd/llama-fast.service`
- **Problem:** logging.basicConfig uses a plain logging.FileHandler('/srv/jarvis/logs/orchestrator.log') with no RotatingFileHandler/TimedRotatingFileHandler, so the file grows unbounded. It ALSO logs full extracted personal facts (store_fact logs content[:80], line 327) and RAG details to disk. The log additionally goes to StreamHandler (stdout), which systemd captures into journald, so every line is duplicated (disk file + journal) - and neither unit sets SyslogIdentifier, StandardOutput, or any journald size cap. On an 8GB/low-disk box this can silently fill disk over time. The logs/ dir also contains benchmark txt files that would be committed.
- **Fix:** Use RotatingFileHandler with maxBytes+backupCount, OR drop the FileHandler and rely solely on journald with SyslogIdentifier= and a journald retention cap. Avoid logging user PII (fact content) at INFO. Add logrotate if keeping the file.

### 77. [MEDIUM] Restart=on-failure does not restart on clean-but-wrong exits; no startup throttling or watchdog; Requires creates restart coupling

- **Location:** `4 (Requires=llama-fast.service), 15-16 (Restart=on-failure, RestartSec=5); llama-fast.service:16-17`
- **Problem:** Both units use Restart=on-failure. If uvicorn exits 0 (e.g. uvicorn.run returns, or a graceful shutdown after an unrecoverable startup condition), systemd will NOT restart it. There is no StartLimitIntervalSec/StartLimitBurst, so a crash-loop (e.g. ChromaDB/embeddinggemma model download failing, or DB locked) restarts every 5s indefinitely, hammering the 2-core CPU. The orchestrator declares Requires=llama-fast.service (line 4): with Requires (vs Wants), if llama-fast is stopped/fails to start, the orchestrator is also stopped - and the ChromaDB embedding model 'google/embeddinggemma-300m' (main.py:208) is downloaded/loaded at import time, which on first boot needs network and RAM and can fail the whole startup. No Type=notify/sd_notify watchdog means systemd considers the service 'started' as soon as the process forks, before the model and DB are actually ready.
- **Fix:** Use Restart=always with StartLimitIntervalSec/StartLimitBurst to cap crash-loops; consider Wants= instead of Requires= if the orchestrator can serve non-LLM endpoints; pre-download the embedding model at deploy time (offline) rather than at import; add a real readiness check.

### 78. [MEDIUM] systemd hardening is minimal and runs as root with full filesystem write access

- **Location:** `1-24 (no User=, no ProtectSystem, no ProtectHome, etc.); llama-fast.service:18-23`
- **Problem:** Both units set only NoNewPrivileges, PrivateTmp, and LimitNOFILE=65536. There is no User=/Group= (so they run as root - confirmed by /srv/jarvis files owned by root and PATH including /root/.local/bin). Missing: ProtectSystem=strict, ProtectHome, ReadWritePaths=, ProtectKernelTunables, ProtectControlGroups, RestrictAddressFamilies, SystemCallFilter, MemoryMax, CPUQuota. The orchestrator executes a subprocess (Piper, main.py:849) and runs arbitrary-ish LLM-driven flows as root with write access to all of /srv and /root. llama-fast deliberately omits ProtectHome because the binary lives in /root/llama.cpp (comment line 19) - i.e. the binary placement forces weaker isolation. No MemoryMax means a runaway (e.g. ChromaDB loading a model, or many queued requests each holding DB connections) can OOM the 8GB box and take down the whole machine.
- **Fix:** Create a dedicated non-root service user owning /srv/jarvis; add ProtectSystem=strict with explicit ReadWritePaths=/srv/jarvis/memory /srv/jarvis/logs; move llama.cpp out of /root so ProtectHome=true is usable; add MemoryMax (e.g. 6G total budget split between llama and orchestrator) and optionally CPUQuota to keep the box responsive.

### 79. [MEDIUM] Every request opens a fresh SQLite connection (multiple per request) with no pooling; WAL set per-connection; threadpool concurrency can cause 'database is locked'

- **Location:** `91-96 (get_db), called repeatedly per request (auth middleware 139, ownership check 815-820, store_message 493, etc.)`
- **Problem:** get_db() opens a brand-new sqlite3.connect on every call and runs PRAGMA journal_mode=WAL + foreign_keys=ON each time. A single /inbox request opens several connections (auth, ownership, get_recent_context, build_messages knowledge+RAG ids, two store_message calls, optional title). Because sync endpoints run in a 40-thread anyio pool and the memory worker also writes concurrently, writers can collide; sqlite3.connect uses the default 5s busy timeout, after which writes raise 'database is locked' (several write paths swallow this with bare except, e.g. _mark_messages_processed lines 395-396, api_keys usage update 160). WAL mitigates reader/writer contention but not writer/writer. Connection setup churn also adds latency on the slow disk path.
- **Fix:** Use a single shared connection or a small connection pool with check_same_thread handling, set PRAGMA busy_timeout explicitly (e.g. 5000+), and serialize writes. Stop swallowing write exceptions silently so lock errors are visible.

### 80. [LOW] -t 2 thread choice underuses SMT but is roughly correct; the -t 2 in BOTH llama and whisper can oversubscribe the 2 physical cores when run concurrently

- **Location:** `11 (-t 2); src/scripts/run_listener.sh:19 (-t 2)`
- **Problem:** i5-2520M is 2 physical cores / 4 threads. For CPU-bound GGML inference without AVX2, -t equal to physical cores (2) is the right default - hyperthreads rarely help and often hurt memory-bound matmul, so -t 2 for llama-server is a reasonable choice (not -t 4). The subtler issue: whisper-command (run_listener.sh:19) ALSO uses -t 2, and Piper TTS plus the orchestrator's own Python threads run on the same 2 cores. When voice is active (whisper listening + a generation + possibly Piper), you have whisper(2) + llama(2) + piper(1) all contending for 2 real cores, so each slows dramatically. The thread count per process is fine; the problem is total concurrent CPU-bound processes on 2 cores with no CPU partitioning.
- **Fix:** Keep -t 2 for llama. Use CPUAffinity/AllowedCPUs (e.g. pin whisper to one logical CPU, reserve cores for llama) or serialize voice capture vs generation so they don't all peak together. Consider -t 4 only if benchmarked faster on this specific no-AVX2 chip (logs/ has benchmarks - validate empirically).

### 81. [LOW] Master API key uses non-constant-time comparison in one path and bare except hides auth/DB failures

- **Location:** `104 (bare except in verify_password), 134 (hmac.compare_digest - good), 160/395 (bare except: pass)`
- **Problem:** Auth itself uses hmac.compare_digest for the master key (line 134) and secrets.compare_digest for passwords (103), which is correct. However verify_password wraps everything in `except:` (line 104) returning False, which is acceptable, but the same bare-except pattern at lines 160, 395-396, 868, 921 hides real failures (DB locked, commit errors) during auth bookkeeping and fact processing, making operational issues invisible. Not a direct auth bypass, but it degrades observability of the security-relevant path (api_key usage tracking) on an internet-adjacent 0.0.0.0 service.
- **Fix:** Replace bare `except:`/`except: pass` with specific exceptions and at least logger.warning so lock/commit failures surface. Keep the constant-time comparisons.


## Rejected during verification (false positives)

- **[security]** Command injection in run_listener.sh via whisper -cmd transcription substitution — The finding assumes whisper-command's `-cmd` flag is a shell command template in which `%s` is replaced by the raw transcription and the result is executed by a shell. I verified this against the actu
- **[security]** Piper subprocess call is argument-list based (safe from shell injection) but processes untrusted LLM output as TTS input — The code at the cited location (main.py:844-855) exactly matches the finding's description, but the finding itself explicitly concludes there is NO vulnerability. It is filed under category "command i
- **[correctness]** api_keys are matched without expiry and trigger a write (usage_count) on every authenticated request, on a connection that is then closed in finally — The finding's central functional claim is refuted by the code. It claims "every request authenticated via an API key issues an UPDATE usage_count on the hot path" and specifically that "under run_list
- **[memory]** get_user_knowledge ordering bug: ORDER BY category, updated_at but grouped by dict insertion order — The code matches what the finding describes, but the finding does not identify an actual bug. Line 261 queries `ORDER BY category, updated_at DESC`, and lines 267-269 group by `r["category"].upper()` 
- **[frontend]** Stored XSS in admin panel via innerHTML with unsanitized username / key description / dates — The DOM sink described is real: admin.html builds rows via innerHTML with unescaped interpolation of u.username/u.role/u.created_at (lines 296-299), the same in showUserModal (306-313), and k.key_stri
- **[ops]** Synchronous Piper TTS subprocess (up to 30s) runs inline inside the /inbox request, blocking the response and a CPU core — The literal code mechanics are accurate: in /inbox, Piper runs via a blocking subprocess.run(timeout=30) after LLM generation and before the single (non-streaming) return, inside a sync `def` route. S
---

# Follow-up review — 2026-06-17 (whole-project, incl. device/edge/agent subsystems)

_4 parallel reviewers + manual verification of the highest-impact claims. Scope adds the newer
`/events` + `/devices/*` endpoints, the Raspberry Pi edge agent (`edge/`), the Windows volume
agent (`clients/volume-agent/`), install/supply-chain scripts, the frontend, and infra/config.
All items are **OPEN / for review** unless noted._

## High

### F1. [HIGH] Device command/event queue: `device_id` is self-asserted, never bound to the API key
- **Location:** `main.py` GET `/devices/commands` (~368), POST `/events` (~394), POST `/devices/volume` (~336); `api_keys` has no device binding.
- **Problem:** Any holder of *any* valid token/API key can (a) drain/read another device's command queue (`?device=laptop` → rows marked delivered, starving the real agent), (b) spoof events as any `device_id` (e.g. `type:"face_seen"`), (c) target any device with volume. Authz checks the *permission* (`_can_control_devices`), not the *device target*.
- **Impact:** Cross-device interception/DoS now; privilege-escalation once `face_seen` drives authorization (planned). Authenticated abuse (needs a valid credential).
- **Fix:** Add `device_id` to `api_keys` (set at mint); enforce `key.device_id == device` on the pull endpoint; bind event provenance; validate the volume `device` target.
- **Status:** OPEN

### F2. [HIGH] Login rate-limit keyed on client IP → global login-lockout DoS behind the subnet router
- **Location:** `main.py` `check_login_rate` (~80), login (~212).
- **Problem:** Behind the Tailscale subnet router, all tailnet logins arrive SNAT'd from `192.168.0.10`, so the per-IP 8/min becomes one shared global bucket. (Verified via topology.)
- **Impact:** Any one client can exhaust the bucket and lock out everyone's login; per-attacker throttling is meaningless.
- **Fix:** Per-username failure throttle + backoff; parse a trusted `X-Forwarded-For` only when behind a real proxy.
- **Status:** OPEN

### F3. [HIGH] Orchestrator runs as root and binds `0.0.0.0`; no app-layer TLS
- **Location:** `systemd/jarvis-orchestrator.service` (no `User=`; `--host 0.0.0.0`). (Verified: no `User=`.)
- **Problem:** Any app/dependency RCE is root in the container; tokens cross the bridge in plaintext. Overlaps the accepted LAN-trust + pending TLS, but running as root is a separate, fixable risk.
- **Fix:** Dedicated non-root `User=`, `ProtectSystem=strict` + `ReadWritePaths=`, `UMask=0077`; complete TLS termination.
- **Status:** OPEN

## Medium

### F4. [MEDIUM] Long-poll handler exhausts the thread pool
- **Location:** `main.py` `pull_device_commands` (~368) — sync `def`, `time.sleep` loop up to 30 s.
- **Problem:** Any user can launch ~40 concurrent polls (varied `?device=`) and starve all endpoints (login, chat). Each iteration also opens a DB connection.
- **Fix:** Make it `async` + `await asyncio.sleep`; run the DB read via `run_in_threadpool`; cap concurrent polls per principal.
- **Status:** OPEN

### F5. [MEDIUM] No Content-Security-Policy (and no HSTS / Referrer-Policy)
- **Location:** `main.py` `_apply_security_headers` (~89).
- **Problem:** Frontend is XSS-safe by construction today, but there's no CSP backstop if that regresses (renderer change / dependency compromise).
- **Fix:** Strict CSP (`default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; …`), `Referrer-Policy: no-referrer`, HSTS at the TLS proxy.
- **Status:** OPEN

### F6. [MEDIUM] SQLite DB is world-readable (`0644`)
- **Location:** `memory/jarvis.db` (verified `0644`); logs `0644`; `jarvis.json` `0644` (no secrets now); `voice_listener.key` correctly `0600`.
- **Problem:** Any non-root local account can read password PBKDF2 hashes + all chat history + knowledge (and `-wal`/`-shm` siblings). Practical risk depends on whether the LXC has non-root users (likely root-only → low), but defense-in-depth.
- **Fix:** `chmod 600` DB + logs; `UMask=0077` in the unit.
- **Status:** OPEN

### F7. [MEDIUM] Unbounded event `data` + no retention on `vision_events` / `device_commands`
- **Location:** `EventRequest.data` (~198, arbitrary dict, no size cap); `schema.sql` (tables never pruned).
- **Problem:** Authenticated disk-fill DoS; monotonic growth on a 1 GB box.
- **Fix:** Cap `data` bytes; periodic purge (mirror the session purge); global request-size limit.
- **Status:** OPEN

### F8. [MEDIUM] Supply chain: unverified downloads + unpinned llama.cpp build
- **Location:** `build_native.sh` (llama.cpp cloned with no pin; whisper.cpp *is* pinned), `download_models.sh` (GGUF + embedding model: no checksum/revision), `piper_setup.sh` (`releases/latest`, no checksum), `fetch_fonts.py` (no checksum; output filename derived from parsed CSS).
- **Problem:** MITM/upstream-compromise → code or model execution on the host.
- **Fix:** Pin commits/revisions; verify SHA-256; require `https`; sanitize derived filenames.
- **Status:** OPEN

### F9. [MEDIUM] Self-scoped stored prompt-injection via fact extraction
- **Location:** `memory.py` `extract_facts_batch` → `user_knowledge` → re-injected into that user's system prompt.
- **Problem:** A user can bias their *own* assistant context. Self-only — no cross-user leak (facts strictly `user_id`-scoped).
- **Fix:** Keep delimited "data not instructions" framing; acceptable given scope.
- **Status:** OPEN (accepted)

### F10. [MEDIUM] Long-lived credentials
- **Location:** `main.py` login (30-day tokens, no rotation, single-token logout); `api_keys` never expire, no self-revoke.
- **Fix:** Shorter/rotating sessions, "revoke all", optional key expiry.
- **Status:** OPEN

## Low

- **F11. [LOW]** Cross-user mutations silently no-op + return `ok` (`rename_session`, `update_fact`, `delete_fact` — `WHERE … AND user_id=?` protects data but returns success on 0 rows, unlike `delete_session` which 403s). Fix: route through an ownership check, 403 on 0 rows. **OPEN**
- **F12. [LOW]** `/system` telemetry (load/CPU/RAM/uptime) exposed to any authed user, not admin-gated. Fix: admin-gate. **OPEN**
- **F13. [LOW]** `admin_delete_user` returns `str(e)` to the client (~582) — internal/schema leak (admin-only). Fix: generic 500 + log. **OPEN**
- **F14. [LOW]** `LoginRequest`/`CreateUserRequest` have no length bounds → multi-MB password forces heavy PBKDF2 (CPU amplification). Fix: `max_length` on username/password. **OPEN**
- **F15. [LOW]** `CreateUserRequest.role` is a free string (footgun; fails closed — only exact `"admin"` is privileged). Fix: enum. **OPEN**
- **F16. [LOW]** Agents send API keys over plaintext HTTP (edge + volume + voice); `face_seen` sends person names (PII) in clear. Accepted LAN-trust. Fix: HTTPS once TLS lands. **OPEN**
- **F17. [LOW]** Key files not created `0600` (`mint-key` → stdout → redirect with default umask; agents don't check perms). Fix: write `0600`; agents warn if group/other-readable. **OPEN**
- **F18. [LOW]** Volume agent trusts server-returned params (no client-side validate/clamp). Defense-in-depth. Fix: validate+clamp client-side too. **OPEN**
- **F19. [LOW]** Agent deps unpinned (`requirements.txt` `>=`, no hashes) — supply-chain drift on the Pi/laptop. Fix: pin + hashes. **OPEN**
- **F20. [LOW]** PBKDF2 100k iterations (OWASP now ~600k). Token SHA-256 correct (256-bit random). Fix: raise iterations. **OPEN**
- **F21. [LOW]** `_safe_exec` swallows broad `"no such"` OperationalError (could mask a real broken migration). Fix: tighten to the specific benign cases. **OPEN**
- **F22. [LOW]** Session token in `localStorage` (XSS→theft, 30-day). Mitigated by no-XSS-by-construction + (proposed) CSP. Fix: HttpOnly cookie + CSRF, or shorter lifetime. **OPEN**
- **F23. [LOW]** In-memory rate-limit dicts never evict keys (`_login_store` grows with distinct IPs) — slow memory growth on the small box. Fix: evict empty buckets / TTL. **OPEN**
- **F24. [LOW]** `run_listener.sh` is misconfigured (see Corrected, below) — won't bridge voice→API as written. Functional bug, not security. Fix: a small Python helper taking the transcript via argv/stdin (no shell) that POSTs JSON. **OPEN**

## Corrected (a re-raised false positive, re-verified)
- **whisper `-cmd` "RCE":** one reviewer re-flagged `run_listener.sh:29` as shell command-injection via `%s`. **Verified against the on-box source** (`whisper/examples/command/command.cpp:86,125,220`): `-cmd`/`--commands` is a *file of allowed words* (guided mode) — no `system()`/`popen()`/`%s` shell substitution. **No RCE.** (Already rejected in the 2026-06-15 audit's false-positive list.) The real issue is functional misconfiguration (F24).

## Verified clean (no action)
SQL fully parameterized (incl. dynamic `IN (…)` — placeholders only); tokens + API keys hashed at rest, no plaintext path; frontend XSS-safe by construction (React nodes only, http(s)-only links with `rel="noopener noreferrer"`, no `dangerouslySetInnerHTML`, no external fetches); CORS locked to `[]`; no committed secrets (double-checked; no deleted-secret history); CI has no injection/secret-leak; volume agent genuinely outbound-only (no shell, no deserialization); SSRF surface config-only; static serving traversal-safe; session-expiry timezones correct.
