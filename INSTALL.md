# Install the kit

The kit runs on your computer, from its own folder. There is nothing to install with pip. You need three
things: Python 3.10 or newer, the code, and language models: two models from different model families, on your
own machine (the default), on a server you already use, or through an API. With one family the app draws the AI maps
but does not judge them.

The same steps, with a Copy button on each command, are on the Install page of the offline site. Where the
commands appear in both places, they are the same text. Where your interview text goes is in
[DATAFLOW.md](DATAFLOW.md).

## 1. Python 3.10 or newer

Check the version.

macOS and Linux, in a terminal:

```
python3 --version
```

Windows, in PowerShell:

```
py --version
```

The Python that comes with macOS can be older than 3.10 (it is 3.9.6 on a stock install). If yours is, install
Python from python.org, or with Homebrew:

```
brew install python
```

On Windows, install Python from python.org. Its installer adds the `py` launcher. On Linux, install a newer
`python3` from your distribution's packages.

## 2. Get the code

Download the repository as a zip and unzip it, or clone it with git. Then open a terminal (PowerShell on
Windows) in that folder, the one that holds `README.md`.

There is no `pip install`. The code runs from the folder, and it uses only Python's standard library.

## 3. Two models from different families

The kit needs one model that draws the maps and a judge from another model family. A model never judges its own
family's output, so the comparison needs a judge from a different family than the reconstructor.

Use the models you already have. In Setup, under "Where the model runs", you enter the address of a model server on
your computer or on your network, such as a lab or university server; an institutional gateway or another API that
speaks the OpenAI chat format, with its key; or Anthropic's API, with its key. You then tell Setup which family each
model is.

As a reference, the study drew the maps with Qwen3.8-Flash-Next and judged them with DeepSeek V4 Flash, with Gemma 4
31B as a second judge family. These names are examples and carry no endorsement.

To serve models on your own computer, install Ollama from ollama.com, or LM Studio from lmstudio.ai and start its
local server. Your own computer is the default, and the authors ran the study only on their own machines. The two commands
below pull a small pair of different families that fits on a laptop, for a quick try; choose larger models when your
machine can hold them.

```
ollama pull gemma3:12b
ollama pull qwen3:8b
```

If you have one computer, the app runs the reconstructor, then asks you to load the judge.

Send interview text to a hosted API only if your ethics approval allows it. The app then accepts only https addresses
and asks you to type the approval it rests on; it keeps that declaration in the app log and prints it on the report.

### Keep Ollama on this computer

Ollama can also serve cloud models. Turn that off, so that no model you pick can send your text away. Set
`OLLAMA_NO_CLOUD=1` and restart Ollama (Ollama's own FAQ describes the variable). The app also refuses any model
that Ollama reports as a cloud model.

macOS, for the Ollama app, then quit and reopen it:

```
launchctl setenv OLLAMA_NO_CLOUD 1
```

macOS or Linux, if you run Ollama from a terminal:

```
OLLAMA_NO_CLOUD=1 ollama serve
```

Windows: set it for your user account, then quit Ollama and start it again from the Start menu:

```
setx OLLAMA_NO_CLOUD 1
```

Linux, if Ollama runs as a systemd service: add `Environment="OLLAMA_NO_CLOUD=1"` under `[Service]` in the file
that this opens, then restart it:

```
sudo systemctl edit ollama.service
sudo systemctl restart ollama
```

### Thinking off

Both models must answer without a thinking phase. The app asks local servers to switch thinking off, and
the connection test in Setup asks each model a short question and checks whether the reply shows thinking. Thinking
shows as a reasoning field in the reply, reasoning tokens, or a `<think>` block. If the test finds it, the
app tells you which model it was, and you choose a variant of that model with thinking off.

The same check runs on every reply during a run. A reply that carries thinking stops the run (exit code 4) and is
not kept, so a run never mixes answers from two different settings.

What is not yet known: whether Ollama honours the switch through its OpenAI-compatible address has not been
tested against a real model by the authors. The connection test is how you find out on your machine.

## 4. Start the app

Run the start command inside the kit folder, the one that holds `README.md`. If the terminal is open in another
folder, move into the kit folder with `cd`, as in this example for a kit unzipped in Downloads.

macOS and Linux:

```
cd ~/Downloads/second-scribe-stage1
python3 -m second_scribe.app
```

Windows, in PowerShell:

```
cd ~\Downloads\second-scribe-stage1
py -m second_scribe.app
```

If the terminal says "No module named second_scribe", it is not in the kit folder. Move into the folder that holds
`README.md` with `cd` and run the start command again.

The terminal prints a link. Open it in your browser (the app also tries to open it for you). Use that link:
the app listens on 127.0.0.1 only, so nothing outside this computer can reach it, and the page works only for
a browser that arrived through the printed link. Opening the address without it shows a plain page that says so.

Useful options:

- `--project PATH` opens the project folder at PATH, or creates it there.
  Keep it in a folder that is not synced to a cloud service. The app warns about the services it recognises by
  folder name, but it can miss one, so check this yourself.
- `--port PORT` fixes the port. Without it the app picks a free one.
- `--no-browser` only prints the link.
- `--version` prints the version and exits.

Stop the app with Ctrl+C in the terminal.

## 5. What next

Setup walks you through the project, your transcripts and maps, the two models and the reference. After a run,
the app shows a link to the Results screen. Press "Show the results of this run" to open it (it stays closed while a
B coding is open); the profile table is at the top. When you want to share numbers, save the Profile for comparison.
It is the only export that passes a privacy check before it is saved, and
[DATAFLOW.md](DATAFLOW.md) says what it holds and what it never holds.

## Problems

- `python3: command not found`: Python is not installed, or not on your path. Go back to step 1. The kit needs Python 3.10 or newer and is tested on Python 3.13 and 3.14; a Python older than 3.10 is not supported.
- `No module named second_scribe`: the terminal is not in the kit folder. Move into the folder that holds `README.md`
  with `cd` (step 4) and start the app again.
- The link does not open: copy it from the terminal into your browser's address bar.
- A model is refused as a cloud model: check `OLLAMA_NO_CLOUD=1` and restart Ollama.
- The page asks you to open the link in the terminal: you opened the address by hand, or the app was restarted.
  Use the link the terminal printed for this start.
