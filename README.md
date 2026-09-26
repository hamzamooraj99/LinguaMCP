<p align="center">
  <img src="logo/logo_lrg.png" alt="LinguaMCP — the model teaches, LinguaMCP remembers" width="180">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/protocol-MCP-6D4AA8.svg" alt="MCP">
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/tests-15%2F15-3E7D5A.svg" alt="15/15 tests">
  <img src="https://img.shields.io/badge/data-100%25%20local-3E7D5A.svg" alt="100% local">
  <img src="https://img.shields.io/badge/license-GPLv3-yellow.svg" alt="GPLv3 License">
</p>

<h3 align="center">Any model can teach a language. LinguaMCP makes sure it still knows your learner next week.</h3>

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python server.py
```

Then point any MCP-compatible client at `python server.py`. No account, no database, no cloud — learner memory is Markdown files under `tutor_data/` that you own and can read.

> **The model teaches. LinguaMCP remembers.** It stores learner profiles, lesson plans, progress, vocabulary, mistakes, scenarios, homework, session checkpoints, and summaries as human-readable files on your machine — and does none of the tutoring itself.

---

## Wait — what *is* this?

Most language-learning chats lose continuity the moment you close the window. The model re-meets your learner every session: no memory of the last mistake, no running vocabulary, no plan for what comes next.

LinguaMCP is the memory layer that fixes that — a small, filesystem-backed MCP server that gives an AI tutor durable, human-readable state **without turning the server into a tutor.**

| LinguaMCP **is** | LinguaMCP is **not** |
|---|---|
| a local memory layer — every learner is a folder of Markdown you own | a cloud service, account, or subscription |
| filesystem-backed and human-readable — the files *are* the source of truth | an opaque database or vector store |
| a set of narrow, validated tools the model calls to read and write context | a "god tool" that does everything through one endpoint |
| deliberately model-led — Python never grades or generates lessons | a tutor, curriculum engine, or proficiency judge |

**Concretely, installing it gives you:** an MCP server exposing eleven validated tools, one Markdown workspace per language under `tutor_data/`, an optional Windows desktop launcher, and an audit trail — with teaching, grading, and curriculum strategy left entirely to the connected model and the user.

---

## The loop

```
  MODEL ──→  initialize_language_profile
              │
              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  READ BOUNDED CONTEXT   read_language_context                 │
  │  active session + summary + profile — never the full          │
  │  archives. The model teaches from a small, current view.      │
  └──────────────────────────────────────────────────────────────┘
              │
              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  TEACH  (the connected model — LinguaMCP stays out of it)     │
  │  lessons, corrections, homework, curriculum strategy:         │
  │  all model intelligence, none of it server-side logic.        │
  └──────────────────────────────────────────────────────────────┘
              │   approved writes only, whitelisted files
              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  PERSIST   write_language_file · save_session_checkpoint      │
  │  profile, progress, vocab, mistakes, homework, delivery       │
  │  drafts — each write validated, path-traversal rejected.      │
  └──────────────────────────────────────────────────────────────┘
              │
              ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  ARCHIVE & COMPACT   append_session_log · compact_language_   │
  │  file · get_language_context_status                           │
  │  permanent timestamped logs on disk; active context stays     │
  │  bounded; the status tool flags files for model-led compaction.│
  └──────────────────────────────────────────────────────────────┘
              │
              ▼
  next session:  read_language_context returns a small, current view —
                 full history archived, ready but out of the way.
```

---

## What it is responsible for

| It **does** | It **does not** |
|---|---|
| create learner profiles from templates | generate lessons |
| return bounded teaching context | grade the learner |
| write approved, whitelisted files | decide curriculum strategy |
| save active-session checkpoints | send emails or WhatsApp messages |
| append permanent session logs | run a web app or database |
| archive and report on compaction | judge proficiency |

Those right-hand responsibilities stay with the connected language model and the user. That separation is the whole design: Python moves bytes safely; the model does the thinking.

---

## Project layout

```text
server.py                 FastMCP server and validated storage operations
templates/                Default Markdown files used during initialization
tutor_data/<language>/    Local learner data, one directory per language
TUTOR_INSTRUCTIONS.md     Tutor-facing model instructions
tests/                    Standard-library verification tests
requirements.txt          Python dependencies
```

Each language gets its own Markdown workspace:

```text
tutor_data/<language>/
  00-profile.md
  01-lesson-plan.md
  02-progress.md
  03-vocabulary.md
  04-mistakes.md
  05-scenarios.md
  active-session.md
  latest-summary.md
  latest-homework.md
  delivery/
    latest-email.md
    latest-whatsapp.md
  sessions/
  archives/
```

---

## Requirements

- Python 3.10 or newer
- An MCP-compatible client
- Local filesystem access to this repository

## Quick start

Create a virtual environment and install dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Run the server with the default stdio transport:

```powershell
python server.py
```

Configure your MCP client to launch `python` with the absolute path to `server.py` as its argument.

---

## Windows desktop launcher

LinguaMCP includes a small WPF desktop controller for starting and stopping the OAuth-enabled HTTP server without PowerShell, WSL, Docker, or a terminal window.

Run this once:

```bat
setup_launcher.cmd
```

The setup creates `.venv/`, installs the dependencies, publishes the launcher, and adds a branded `LinguaMCP MCP` shortcut to the current user's desktop. Before opening the launcher, set these Windows user environment variables and reopen the launcher:

- `LINGUAMCP_OAUTH_PASSWORD` — the password used to approve a client.
- `LINGUAMCP_OAUTH_CLIENTS_FILE` — a private JSON file listing the approved client ID and its exact callback URL.
- `LINGUAMCP_PUBLIC_BASE_URL` — the public HTTPS origin for this server, without a path.

The callback must come from the OAuth client registration you intend to use. Do not guess it or add a broad wildcard. The launcher checks these settings before it starts the server.

Open the shortcut to:

- start the FastMCP server in OAuth HTTP mode
- stop the running server and its child process
- see the current process state
- inspect recent server output

The launcher runs this command without opening a terminal window:

```powershell
python server.py --http --oauth --allow-writes
```

The launcher inherits the settings without storing them in the repository. Closing the launcher stops the server.

---

## HTTP mode

For clients that connect to an MCP endpoint over HTTP:

```powershell
python server.py --http
```

The default endpoint is:

```text
http://127.0.0.1:8000/mcp
```

HTTP mode is **read-only by default.** To let the client update learner files, opt in explicitly:

```powershell
python server.py --http --allow-writes
```

To bind to another interface, for example from Docker or another local-network machine:

```powershell
python server.py --http --host 0.0.0.0 --port 8000
```

To use a different endpoint path:

```powershell
python server.py --http --path /lingua
```

<details>
<summary><b>Authentication</b> — bearer tokens and single-user OAuth for exposed endpoints</summary>

For public tunnels or non-local access, require a bearer token:

```powershell
$env:LINGUAMCP_AUTH_TOKEN = "replace-with-a-long-random-secret"
python server.py --http --allow-writes --require-auth
```

Clients must then send:

```text
Authorization: Bearer replace-with-a-long-random-secret
```

For a public OAuth client, enable the built-in flow with a deployment-owned registry. The registry maps one client ID to exact registered callback URLs:

```powershell
$env:LINGUAMCP_OAUTH_PASSWORD = "replace-with-a-long-random-password"
$env:LINGUAMCP_OAUTH_CLIENTS_FILE = "C:\private\linguamcp-oauth-clients.json"
$env:LINGUAMCP_PUBLIC_BASE_URL = "https://<your-tunnel-host>"
# Optional. The default is tutor_data/.oauth/state.sqlite3.
$env:LINGUAMCP_OAUTH_STATE_PATH = "C:\private\linguamcp-oauth-state.sqlite3"
python server.py --http --oauth --allow-writes
```

The JSON file must have this shape. Replace both example values with the client ID and callback URI from the actual client registration:

```json
{
  "clients": {
    "client-id-from-registration": {
      "redirect_uris": ["exact-callback-uri-from-registration"]
    }
  }
}
```

Client IDs may be HTTPS URLs, as they are for some ChatGPT connectors. Copy the
exact registered ID and callback; the server matches both literally. On a
systemd host, set `LINGUAMCP_OAUTH_CLIENTS_FILE` and
`LINGUAMCP_PUBLIC_BASE_URL` in the service environment and make the JSON file
readable by the service account. OAuth startup fails if either setting is
missing. The public base URL is the HTTPS origin without `/mcp`.

Use your public tunnel host with these paths:

```text
Server URL: https://<your-tunnel-host>/mcp
Auth URL:   https://<your-tunnel-host>/oauth/authorize
Token URL:  https://<your-tunnel-host>/oauth/token
```

Configure the client with its registered client ID, the URLs above, no client secret, and PKCE method `S256`. The server rejects unregistered IDs, callbacks that differ by even one character, and other PKCE methods. During approval, enter the value from `LINGUAMCP_OAUTH_PASSWORD`.

OAuth discovery metadata is exposed at:

```text
/.well-known/oauth-authorization-server
/.well-known/oauth-protected-resource
```

OAuth grants are stored in a private SQLite file outside language folders. Existing in-memory grants are not migrated, so OAuth clients must authorize again after upgrading. Access tokens last 24 hours; refresh tokens last up to 30 days and rotate on use. Replaying an old refresh token revokes that grant family, so the client must authorize again. To revoke all saved OAuth grants locally, stop the server and run:

```powershell
python server.py --revoke-oauth-grants
```

The revoke command exits without starting the server. It uses `LINGUAMCP_OAUTH_STATE_PATH` when set. Static bearer-token authentication remains a separate option and does not use this OAuth database. OAuth mode uses a static client registry; it does not implement dynamic client registration.

</details>

---

## MCP tools

| Tool | Purpose |
| --- | --- |
| `initialize_language_profile` | Create a complete language profile from templates and supplied learner details. |
| `read_language_context` | Return the memory protocol and bounded active teaching context, including an optional current contract. It excludes custom notes, permanent logs, and archives. |
| `read_language_file` | Read a context file in pages that share one content hash. |
| `write_language_file` | Replace one allowed Markdown file, including homework and delivery drafts, after checking its latest file hash. |
| `list_language_notes` | List custom Markdown notes stored for a language. |
| `read_language_note` | Read one custom Markdown note before using or updating it. |
| `write_language_note` | Create or replace a custom Markdown note displayed under **Other** in the viewer. |
| `append_session_log` | Save a paused session and update `latest-summary.md` while preserving `active-session.md`. |
| `list_language_sessions` / `read_language_session` | Find older session logs and read them in version-checked pages. |
| `finalize_lesson` | Check the six-file update checklist, then save progress, homework, summary, session log, checkpoint reset, and contract completion as one recoverable operation. |
| `save_session_checkpoint` | Replace the concise active-session state during a long or unfinished lesson. |
| `get_language_context_status` | Report character counts and recommend compaction when files grow large. |
| `compact_language_file` | Archive a complete cumulative file and replace it with a concise model-supplied version. |
| `list_language_file_archives` | List validated historical archive versions for one file. |
| `read_language_file_archive` | Read one validated archive file on demand. |
| `create_lesson_contract` | Create or intentionally replace the temporary scope and evidence rules for the current lesson. |
| `read_lesson_contract` | Read contract identity and bounded content pages. |
| `update_lesson_contract_state` | Record teaching-state changes and clear an older assessment. |
| `assess_lesson_contract` | Record evidence for every required skill and return a mastery record when the completion rules pass. |
| `list_languages` | Return available language directories alphabetically. |

Language identifiers are normalized to lowercase and may contain only letters, digits, and hyphens. Learner-memory filenames come from a fixed whitelist. Custom notes are restricted to simple `.md` filenames directly inside the language's `notes/` directory, so callers cannot write arbitrary paths.

---

## Data model

LinguaMCP keeps active context small and recoverable:

- `active-session.md`, `latest-summary.md`, `latest-homework.md`, and delivery drafts are bounded because they are **replaced**, not appended.
- `sessions/` stores **permanent** timestamped lesson logs.
- `archives/` stores **complete previous versions** of cumulative files before replacement or compaction.
- `current-lesson-contract.md` stores only the current temporary lesson contract. It stays out of ordinary writes and compaction; creating the next contract replaces it after the previous one is completed.
- `.backups/`, `.transactions/`, and `.operations/` hold private recovery copies, prepared saves, and retry receipts. They are hidden from the viewer and learner-memory tools.
- `notes/` stores optional custom Markdown references, which the viewer displays under **Other**. Use `list_language_notes` and `read_language_note` before replacing an existing note.
- Profile, lesson plan, progress, vocabulary, mistakes, and scenarios can grow over time, so the status tool flags large files for model-led compaction.

Compaction is deliberately model-led: Python archives the original and writes the replacement supplied by the AI. It does not decide what learning content matters.

Reads return whole-file SHA-256 versions and writes require the version from the read used to prepare the replacement. A stale write is rejected so it cannot silently replace newer notes. The tutor rereads and merges after a conflict. Large files can be read in 16,000-character pages; full context is returned only when it fits the 64,000-character response limit. Cumulative memory is limited to 128,000 characters per file, session/checkpoint/homework/summary and delivery files to 16,000 characters, notes to 128,000 characters, and a contract to 128 KiB.

The main failure responses are deliberate: a **Version conflict** means a file or contract changed after it was read; reread and merge. `context_too_large` returns no partial lesson context; use the status tool and bounded file reads. `recovery_required` means an interrupted save must be completed by a writable server before using that language again; keep the affected files and recovery folders intact while it recovers.

Create a contract before teaching a new lesson. It sets required skills, their evidence level, the lesson boundary, deferred topics, and the completion rule. The tutor assesses every skill from what the learner actually did. The server checks the assessment structure and readiness but cannot confirm that the reported evidence is true. The passing mastery block belongs in the full `02-progress.md` replacement. Older workspaces without a contract remain readable and can still use finalization after adopting its new version and operation fields.

Only `finalize_lesson` completes a contracted lesson and resets the checkpoint. A paused `append_session_log` keeps the checkpoint so the tutor can resume later. Finalization and standalone logging both need a client-generated UUID operation ID. A retry must reuse that ID and the exact original payload; a deliberate later save needs a new ID. One writable server is allowed per data root so its recovery journal can finish interrupted saves safely.

### Save-call compatibility

Existing write calls now need the exact file version returned by context or a file read. `initialize_language_profile` needs `expected_versions` when replacing an existing profile or plan. `append_session_log` takes `expected_versions` for `latest-summary.md` plus `operation_id`. `finalize_lesson` takes hashes for all six cumulative files, homework, latest summary, checkpoint, and the contract when present, plus `operation_id`; a contracted lesson also needs `expected_contract_version`, which is the contract lifecycle token, not its file hash. A same-ID retry must send the original versions and unchanged payload.

### Lesson contract format

The contract file contains a JSON metadata fence followed by tutor-authored Markdown. Its body must have exactly one nonempty section for each heading: `Lesson Purpose`, `Required Vocabulary and Constructions`, `Prior Material to Interleave`, `Suggested Teaching Progression`, `Retrieval and Practice Requirements`, `Scope Boundaries`, `Deferred Topics`, `Optional Enrichment`, `Critical Gaps`, and `Completion Rule`.

The contract tools accept these fields:

```text
create_lesson_contract(language, lesson_id, lesson_title, contract_markdown, competencies, expected_version="")
read_lesson_contract(language, offset_chars=0, limit_chars=16000, expected_file_version=None)
update_lesson_contract_state(language, expected_version, competency_updates)
assess_lesson_contract(language, expected_version, competencies, unresolved_critical_gaps, completion_rule_met, carry_forward_weaknesses)
finalize_lesson(language, session_markdown, homework_markdown, updates, unchanged_files, expected_versions, operation_id, expected_contract_version="")
```

Each `competencies` entry has exactly `id` (lowercase ID), `criterion` (text), `critical` (boolean), and `required_evidence` (`recognized`, `supported`, `independent`, or `delayed_or_mixed`). Each state update has `competency_id`, `status`, and `note`; status is `not_introduced`, `introduced`, `practising`, `demonstrated`, `weak`, or `delayed_retest_required`. Each assessment entry has `competency_id`, `criterion_met` (boolean), `evidence_level` (`not_observed`, `introduced`, `recognized`, `supported`, `independent`, or `delayed_or_mixed`), and a short `evidence` explanation. Assess every competency exactly once. `unresolved_critical_gaps` uses competency IDs; `completion_rule_met` is boolean; `carry_forward_weaknesses` is a list of short notes.

File hashes and contract tokens are separate: create, state, and assessment calls use the contract's current `version_token`; subsequent finalization also sends the latest `file_version` in `expected_versions`. A state update clears the previous assessment. A completed contract remains readable with status `completed`; create the next lesson using its current token, which gives the new lesson a new identity and resets its states and assessment.

---

## Security posture

LinguaMCP is designed to be boring and local:

- all learner data stays under `tutor_data/`
- all text is read and written as UTF-8
- path traversal is rejected
- write targets are whitelisted
- HTTP writes require `--allow-writes`
- bearer-token auth is available for exposed HTTP endpoints
- tool calls are audited **without logging Markdown content**

Audit logs are written to:

```text
tutor_data/audit-log.jsonl
```

Use `--read-only` to block write-capable tools in any transport, and `--no-audit` to disable local JSONL audit logging.

---

## Verification

Run the test suite:

```powershell
python -m unittest discover -s tests -v
```

The tests use temporary learner folders and OAuth databases to check versions, backups, interrupted-save recovery, session history, lesson assessment and completion, redirect/PKCE validation, token rotation/revocation, and viewer navigation. They do not alter learner folders or production OAuth data.

Run the viewer stale-navigation checks with:

```powershell
node --test tests/test_viewer_navigation.mjs
```

---

## FAQ

**How is this different from just chatting with an AI tutor?**
A chat has no memory of your learner between sessions. LinguaMCP adds the state a chat can't hold: a durable profile, a running record of vocabulary and mistakes, permanent session logs, and a bounded context the model can re-read next time. The teaching is the easy part — continuity is what's missing.

**Does it teach or grade the learner?**
No. On purpose. Lesson generation, correction, curriculum strategy, and proficiency judgment all stay with the connected model. Python only reads and writes files safely. That boundary is enforced by the tool set, not by convention.

**Where does my data live?**
Under `tutor_data/`, one folder per language, as plain Markdown you can open, edit, diff, or delete. There is no database, no frontend, no external sender, and no automatic backup. Nothing leaves your machine unless you deliberately expose the HTTP endpoint.

**Can I expose it over the network?**
Yes — HTTP mode with `--allow-writes`, protected by a bearer token or the built-in single-user OAuth flow for clients like ChatGPT connectors. Writes are opt-in; read-only is the default.

**Won't the files grow forever?**
The status tool reports character counts and recommends compaction. When you agree, the model supplies a concise replacement and Python archives the full original under `archives/` — nothing is lost.

---

## Contributing and support

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change and follow the [Code of Conduct](CODE_OF_CONDUCT.md).

Use [SUPPORT.md](SUPPORT.md) for support guidance. Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

LinguaMCP is free software licensed under the [GNU General Public License v3.0](LICENSE).

---

<details>
<summary><b>Design principles</b></summary>

LinguaMCP favors:

- simple files over databases
- explicit tools over broad "god tools"
- local control over hosted state
- Markdown over opaque storage
- model intelligence over server-side teaching logic

The included `tutor_data/german/` directory is example data and can be edited or removed. There is no database, frontend, external sender, or automatic backup.

</details>

---

<sub>*A <b>lingua franca</b> is the shared tongue that lets strangers understand each other. LinguaMCP is the shared memory that lets a model and a learner pick up exactly where they left off.* · Built on FastMCP · GPLv3</sub>
