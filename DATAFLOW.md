# Where your data goes

Runs on your computer. By default nothing leaves your machine. If your ethics approval allows a cloud model, you
choose where the text goes, and the app records it.

This page says what that means, where each claim can be checked, and gives your data steward a ten-minute check.
Each claim is followed by an HTML comment that names the code behind it as `file:name`, so a reviewer can open
the file and look.

## What is on your machine

The app keeps one folder per project, in the place you choose.

- `second-scribe-project.json` holds the project settings, file paths and file hashes.
- `inputs/` holds copies of your transcripts and maps. These are participant text. Transcripts must be in English (the
  quote check keeps only ASCII letters; non-English text is a known limit).
- `reference/` holds the reference maps. `coding/` holds your coding marks, with the quoted text.
- `runs/` holds one folder per run. Its `logs/run.jsonl` and `work/` hold participant text: the requests sent to
  the models and the replies.
- `views/` holds the data the screens draw from, which includes quotes.
- `logs/app.jsonl` is the app's own log: what you imported and removed (as file hashes), confirmed, chose and
  exported. A removal adds a record; earlier records are never changed. It names participants by id. No record
  written so far holds participant text.
- `private/` holds the map from file names to participant ids, and the standard for speaker labels, whose role rules
  hold parts of file names. It is never shown and never exported.
- `exports/` holds what you export: the report folder, the Profile for comparison (`profile-<run>.json` and
  `.csv`, one pair for each run), a methods paragraph (`methods-<run>.txt`) and the run record
  (`run-record-<run>.json` and `.md`; the report folder holds its own copy as `run-record.json` and `run-record.md`). A `.staging/` folder keeps the working copies the Profile preview made.

<!-- cite: second_scribe/app/project.py:LAYOUT, second_scribe/audit/applog.py:APP_RECORD_TYPES, second_scribe/app/coding.py:lock, second_scribe/app/importer.py:remove_file, second_scribe/app/importer.py:STANDARD -->

Keep this folder on the machine that ran the study.

## What the app talks to

There are three connections, and no others.

1. **Your browser and the app, on this computer.** The app listens on 127.0.0.1 only, on a free port. There is
   no option to listen on a network address.
   <!-- cite: second_scribe/app/server.py:HOST, second_scribe/app/server.py:serve, second_scribe/app/__main__.py:build_parser -->
2. **The app and the model servers you set up.** By default these are on this computer. Two parts of the app open
   these connections: the model clients of a run, and the Setup screen's own model list, connection test and model
   swap. Both use an opener with no proxy and no redirect, and neither contacts an address the app has not allowed.
   <!-- cite: second_scribe/llm.py:_OPENER, second_scribe/llm.py:HTTPClient, second_scribe/anthropic_client.py:AnthropicClient, second_scribe/app/models.py:_OPENER, second_scribe/app/models.py:connection_test, second_scribe/app/jobs.py:swap_models -->
3. **A name lookup, if a model address is a name.** If you give a model address as a name rather than a number,
   your operating system resolves it, once for the app's check and again for each request. For a name that is not
   on this computer, the system may ask your network's DNS server. The name is sent, never interview text. A
   numeric address such as `127.0.0.1` needs no lookup.
   <!-- cite: second_scribe/llm.py:_resolve, second_scribe/llm.py:endpoint_problem -->

The app opens the printed link by handing it to your default browser. It makes no other outside request: no
update check, no counter, no fonts from the web.
<!-- cite: second_scribe/app/server.py:serve, second_scribe/app/server.py:CSP -->

The Setup screen's "Choose folder…" button starts your system's own folder window on this computer: `osascript` on
macOS, PowerShell on Windows, and `zenity` or `kdialog` on Linux. The command is fixed, carries no text from the page,
and is not run through a shell. The app reads back only the path you pick, which fills the folder field. If no folder
is chosen within two minutes, the app stops the window. Nothing from it leaves the computer.
<!-- cite: second_scribe/app/folder_dialog.py:command, second_scribe/app/folder_dialog.py:choose, second_scribe/app/folder_dialog.py:TIMEOUT, second_scribe/app/routes/folder_dialog.py:choose_folder -->

The Export screen's "Open" and "Show in folder" buttons hand a saved export to your system's own opener on this
computer: `open` on macOS (`open -R` to show the file in its folder), Explorer on Windows, and `xdg-open` on Linux. The
command is fixed and is not run through a shell. Its only other text is the path of a file that the app has checked
is inside the project's `exports` folder. The app waits at most 15 seconds for the opener to take the file. Nothing
leaves the computer.
<!-- cite: second_scribe/app/opener.py:command, second_scribe/app/opener.py:open_path, second_scribe/app/opener.py:WAIT, second_scribe/app/routes/export.py:open_export -->

How the app keeps other pages and programs out:

- A request must name `127.0.0.1:<port>` or `localhost:<port>` in its Host header, or it gets a 403. This stops
  a web page from reaching the app by a name that points to your computer.
  <!-- cite: second_scribe/app/server.py:Handler._check_host -->
- The printed link carries a random token. The app trades it for a session cookie (HttpOnly, SameSite=Strict) and
  removes it from the address. The app page and every call for data (`/api`) need that cookie. The fixed files the
  page is built from (code, styles, fonts) do not.
  <!-- cite: second_scribe/app/server.py:Handler._root, second_scribe/app/server.py:AppServer.setup_launch -->
- Every POST also needs a second random token in a header, and an `Origin` header, if sent, must be the app's own.
  <!-- cite: second_scribe/app/server.py:Handler._check_csrf -->
- The app sends no CORS headers, so no other site may read its answers. Only GET and POST are accepted.
  <!-- cite: second_scribe/app/server.py:Handler._route -->
- The app serves only the files in its `static/` folder, from a list built at start, and never the project folder.
  <!-- cite: second_scribe/app/server.py:build_static_allow -->
- It writes no request log, because a request line can hold the launch token.
  <!-- cite: second_scribe/app/server.py:Handler.log_message -->
- Every response carries this content security policy (CSP), which tells the browser to load code, styles,
  fonts, images and connections only from the app itself, and not to show the app inside another page:
  `default-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'`
  <!-- cite: second_scribe/app/server.py:CSP, second_scribe/app/server.py:Handler.end_headers -->
- The offline site (Explain, The study, Try it, Install) has no server. Each page carries a policy as a `<meta>`
  tag, which stops the pages' code from opening any connection (`connect-src 'none'`):
  `default-src 'self'; connect-src 'none'; img-src 'self' data:; font-src 'self'; style-src 'self'; script-src 'self'`
  Its data are JavaScript files next to the pages. Try it shows a recorded run; no model is called.
  <!-- cite: second_scribe/app/build_site.py:CSP, second_scribe/app/build_site.py:CSP_META -->

## Which model addresses the app accepts

Before any request of a run on real data, the app checks every model address. It accepts:

- this computer: the whole `127` block and `::1`;
- a private network (the RFC 1918 blocks): every address beginning `10`, `172.16` to `172.31`, or `192.168`, only
  after you record an opt-in for that model role and that host;
- Tailscale: the `100.64` to `100.127` block (`100.64/10`), with the same opt-in.

It refuses every other address, unless the project's remote allow-list (next section) names that exact endpoint over
https. Nothing else turns the check off. The check is made when each run process starts, before it sends any request. For a name, every address the name resolves to
must be in those ranges, and a name that does not resolve is refused. The app sends no model request through a
proxy, even if your system has one, and it refuses to follow a redirect.
<!-- cite: second_scribe/llm.py:guard_real_clients, second_scribe/llm.py:endpoint_problem, second_scribe/llm.py:_LOCAL_NETS, second_scribe/llm.py:_OPENER, second_scribe/pipeline.py:run -->

The app adds two checks to the kit's own address check. Setup does not contact a private-network or Tailscale
address until you record an opt-in for that model role and host, and does not contact any other non-local address
until you tick the declaration and type its basis (next section). This holds for the model list, the connection
test, the model check and every run, and the app refuses to write a run's config if either record is missing.
<!-- cite: second_scribe/app/models.py:contact_problem, second_scribe/app/models.py:log_optin, second_scribe/app/models.py:log_declaration, second_scribe/app/models.py:require_declarations, second_scribe/app/routes/models.py:_contact, second_scribe/llm.py:guard_real_clients -->

Mind the second and third kind. If you point a model at another computer on your network, the interview text
goes to that computer. The run certificate lists any network opt-in the project recorded.
<!-- cite: second_scribe/certificate.py:_app_entry, second_scribe/certificate.py:_run_entry -->

Each model call the audit log records carries the host and port it went to, never the password or the key. The
determinism check re-sends up to 20 logged requests per model without logging them again, and the Setup checks keep
their own logs.
<!-- cite: second_scribe/llm.py:log_call, second_scribe/llm.py:endpoint_host -->

The model check you run in Setup, before your own data, sends six of the synthetic food-truck interviews that ship
with the app, not yours.
<!-- cite: second_scribe/model_check.py:CHECK_CONFIG -->

## The remote option

For researchers whose ethics approval allows a large-lab model, for example under a university zero-retention
agreement. Local stays the default, and this is your own choice.

**An https-only allow-list.** A run on real data may send text to a non-local address only if its project config
lists that endpoint. Each entry names a `role` (`recon`, `judge` or `judge2`), a `host` (with the port, if the address has
one) and a `provider` (`openai_compat` or `anthropic`). The run is refused unless the endpoint matches an entry on all three. The
address must use `https`: a remote `http` address is refused, and no config or environment setting changes that.
<!-- cite: second_scribe/llm.py:guard_real_clients, second_scribe/llm.py:remote_entry, second_scribe/llm.py:REMOTE_SCHEMES, second_scribe/toolcfg.py:check_tool, second_scribe/toolcfg.py:PROVIDERS -->

**A declaration for each remote role.** Each entry also carries `declaration_sha256`, the hash of the declaration
you made for that role: a tick box and the basis in your own words, for example "REB #1234 approves the
Anthropic API under the institution's zero-retention agreement". The config is refused without it, the config hash
covers it, and the app log has a `declaration` record type that the run certificate prints with the provider and
host.
<!-- cite: second_scribe/toolcfg.py:check_tool, second_scribe/audit/applog.py:APP_RECORD_TYPES, second_scribe/certificate.py:_app_entry -->

**Keys stay in memory, never on disk.** You paste an API key into the app, or name an environment variable that
already holds it.

- The app keeps a pasted key in its own memory only. The class that holds it has no method that saves the
  key, and it refuses to be copied or pickled. The only method that gives the key back is `env_for`. The app uses
  it to build the environment of a kit process (a run, the model check, the time estimate), and to send the key in
  one request header when Setup lists a server's models or tests the connection.
  <!-- cite: second_scribe/app/api.py:Secrets, second_scribe/app/api.py:Secrets.__reduce_ex__, second_scribe/app/api.py:Secrets.env_for, second_scribe/app/routes/models.py:_key -->
- A run that needs a key gets it through the environment of its own process, together with the name of the
  variable. Neither the key nor the name is written to the project folder.
  <!-- cite: second_scribe/app/api.py:Secrets.env_for, second_scribe/llm.py:ModelConfig -->
- The key is read when a request is made, and goes into one request header and nowhere else. Error text is cleaned
  of it before it is logged or shown.
  <!-- cite: second_scribe/llm.py:HTTPClient._request, second_scribe/llm.py:_scrub, second_scribe/anthropic_client.py:AnthropicClient._request -->
- While a run is going, the key sits in that process's environment, where your own account's other programs can
  read it. If you name a variable that your own shell profile sets, the key is in the file where you put it.

**What the provider receives.** For a remote reconstructor, the interview text. For a remote judge, the quotes and
map items it labels. The no-interview reading sends no interview text. What the provider does with what it
receives is set by your agreement with them, not by this app. The app cannot inspect the weights of a remote
model, and the provider may change the model behind an id. For this reason the run certificate records the
model id you configured at the provider, warns when the id has no date or version (it may be an alias that moves), and says
"remote model: weights not inspectable; the provider may change the model behind this id".
<!-- cite: second_scribe/certificate.py:REMOTE_LINE, second_scribe/certificate.py:is_alias_id, second_scribe/certificate.py:_run_entry -->

**What still applies.** When a run uses the two-family judge rule, which the app's model check always does, the judge must be
from a different model family than the reconstructor, remote or local.
The check that thinking is off, which uses a switch only local servers understand, does not apply to a remote
endpoint. The Anthropic client sends no extra fields, and a reply with a thinking block stops the run.
<!-- cite: second_scribe/llm.py:guard_real_clients, second_scribe/anthropic_client.py:AnthropicClient.complete -->

## What the run certificate attests

The run certificate is a document the app builds from your logs. It attests the integrity of the logs and what was
run. Its own opening line says so:

> This document attests the integrity of the logs and what was run. It does not attest that the results are right.

<!-- cite: second_scribe/certificate.py:ATTESTS -->

Concretely, it lists, for each run folder it is given (from the command line, every run folder including failed and
interrupted ones; from the Export screen, the run you export):

- a fresh check of the audit log's hash chain, with the date;
- the chain head;
- the hash of the code that ran (its Python, JSON and text files; the browser files are covered by the release
  hash in step 5 below), the config hash, and the prompt-set hash;
- the model ids and, where the server gave one, each model's digest;
- any remote endpoint with its declaration hash;
- for the app log, the opt-ins, the declarations and where a coding was locked before the reveal.

<!-- cite: second_scribe/certificate.py:build_certificate, second_scribe/certificate.py:_run_entry, second_scribe/certificate.py:_app_entry, second_scribe/codehash.py:code_sha256 -->

It reads each run's logs (which contain the requests and replies) and its `config.json`, and writes only hashes,
ids, outcomes and record positions, plus the hosts, model ids and family names you set up. To check the model digests it re-reads your input files to recompute the config
hash. The only text of yours on the run record is the ethics basis you typed for a cloud role; no transcript or map
text is copied into it.
<!-- cite: second_scribe/certificate.py:build_certificate, second_scribe/certificate.py:remote_sentences -->

What it does not say: it does not say the input is what you think it is, that a model's answers are right, or
that a finding holds. Its limit line reads: "The chain can reveal edits when checked against a trusted head; it does not show what the
input was, nor that the models' answers are right." Full verification needs the logs,
which hold participant text, so you run it on your own machine. When a coding was locked, the run certificate adds
that blindness is enforced by the interface and recorded in the log order, and is not cryptographic.
<!-- cite: second_scribe/certificate.py:LIMIT_TEMPLATE, second_scribe/certificate.py:BLINDNESS_LIMIT -->

## What can leave your machine

Only what you take out yourself.

- **The Profile for comparison** is the file made to be shared. It holds counts, rates and intervals, plus the
  run's hashes, model names and settings. The exporter refuses to write it for fewer than 10 people. It also
  refuses if its privacy scan finds a field whose name contains interview, participant, role, entry, record_hash,
  map_id or pair_id; a participant
  id, interviewer code or role name as a value; a 64-character hash outside the run block; or text that breaks the
  string rules (short codes, no sentences). And it refuses if the run's log does not verify. Before the app saves
  it, a second check (the publish gate) allows only known fields and refuses any of the project's own names.
  <!-- cite: second_scribe/profile_export.py:write_profile, second_scribe/profile_export.py:build_run_block, second_scribe/profile_export.py:privacy_scan, second_scribe/profile_export.py:K_FLOOR, second_scribe/profile_export.py:FORBIDDEN_KEY_FRAGMENTS -->
- **The report** stays in `exports/`. It shows your results with your quotes. Treat it as participant text.
- **Sync folders.** If the project folder is inside iCloud Drive (including Desktop and Documents when iCloud
  syncs them), OneDrive, Dropbox, Google Drive, Box, pCloud, Nextcloud, MEGA, Sync.com or SharePoint, or anywhere
  under macOS's `Library/CloudStorage`, the sync program sends participant text out. The app detects
  this, blocks the project, and goes on only after you acknowledge the warning, which it logs. The check reads
  folder names, so it can miss a service it does not know or a synced folder you renamed. Confirm yourself that
  the project folder is not synced.
  <!-- cite: second_scribe/app/project.py:sync_folder_kind, second_scribe/app/project.py:Project.blocked, second_scribe/app/project.py:Project.ack_sync -->
- **Browser extensions** can read the text of any page, including this one. Use a browser profile without
  extensions for the app.
- **Chrome's Enhanced spell check** sends the text you type into web pages to Google. The basic spell check does not.
  The app's text boxes are web page text boxes. Check Settings, Languages, Spell check in Chrome.
- **Backups and other software on your computer** copy the project folder like any other folder.

## Ten-minute check for a data steward

1. **Airplane test.** Turn Wi-Fi off and unplug the network cable. Start the app, open the link and go through
   Setup with local models; open the offline site. Everything works. (Downloading a model with `ollama pull` is a separate step
   that needs the network and involves no interview text. A remote model stops working, which is the point.)
2. **Listening sockets.** While the app runs, on macOS or Linux:
   ```
   lsof -iTCP -sTCP:LISTEN -P -n | grep -i python
   ```
   The app's line shows `127.0.0.1:<port>`, never `*` and never a network address. On Windows, in PowerShell:
   ```
   netstat -ano | findstr LISTENING
   ```
3. **No outside address in the app's code, styles and pages.** From the folder that holds `README.md`:
   ```
   grep -rE "https?://" --include="*.js" --include="*.css" --include="*.html" second_scribe/app/static second_scribe/app/pages
   ```
   It prints nothing. The check covers the files a page runs or loads. It leaves out the font licence and source
   notes in `static/fonts/` (`SOURCES.md` and `OFL-*.txt`) and the spreadsheet template in `static/templates/`,
   because licence and source files name where they come from: a licence names its publisher, a source note names
   the page the font came from, and the XML inside a spreadsheet file names the standard it follows. That is text,
   not a request, and no page loads those files. To see every mention anyway, run
   `grep -rE "https?://" second_scribe/app/static` yourself; the hits are those files and nothing else.
4. **The content security policy.** With the app running, in another terminal (use your port):
   ```
   curl -si http://127.0.0.1:PORT/ | grep -i content-security-policy
   ```
   It prints the policy quoted above. (This request has no session, so the app answers 401 with the same headers.)
5. **The release.** Compare the SHA-256 of the file you downloaded with the one published with that release.
   macOS or Linux:
   ```
   shasum -a 256 second-scribe.zip
   ```
   Windows, in PowerShell:
   ```
   Get-FileHash -Algorithm SHA256 second-scribe.zip
   ```
   To compare the code a run used, print the hash of the code you have and compare it with `Code sha256` in the run certificate:
   ```
   python3 -c "from second_scribe.codehash import code_sha256; print(code_sha256())"
   ```
   <!-- cite: second_scribe/codehash.py:code_sha256 -->
6. **Where the text went.** In a project folder, list the hosts the model requests went to:
   ```
   grep -rhoE '"endpoint_host":"[^"]*"' runs/*/logs/run.jsonl | sort | uniq -c
   ```
   For a local setup, every line starts with `127.0.0.1` or `localhost`, followed by the port. Setup checks and
   the determinism re-sends keep their own logs or none.
   <!-- cite: second_scribe/llm.py:log_call -->
7. **The logs and the run certificate.** Build the run certificate from the project folder and read it:
   ```
   python3 -m second_scribe.cli certificate --run runs/RUN_FOLDER --app-log logs/app.jsonl --out exports/run-record.json
   ```
   It prints whether every log verifies, and writes `run-record.json` and `run-record.md`. Remember what it
   attests: what was run and whether the logs verify (the chain can reveal edits when checked against a trusted head),
   never that the results are right.
   <!-- cite: second_scribe/certificate.py:cmd_certificate -->

## What this page does not cover

The model programs you install (Ollama, LM Studio) are not part of this kit, and what they do on your
network is theirs. Turn on `OLLAMA_NO_CLOUD=1` as INSTALL.md says. The page also says nothing about your
operating system, your browser's own sync, or your institution's network.
