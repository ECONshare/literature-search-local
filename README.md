# LitReview on your own computer

LitReview helps you review literature by research question. You write a question. It
searches OpenAlex, Semantic Scholar and Crossref, picks the most relevant papers, fetches
their open-access PDFs, highlights the passages that bear on your question, and writes a
cited answer. Hover over a citation to see the exact passage behind it, and click it to
open the PDF at that highlight.

This version runs entirely on your computer, on Windows or macOS. It works with the Claude
subscription you already have. **Claude does the reading and writing, through Claude
Desktop or Claude Code.** There are no API keys to buy and no other AI services.
Everything you collect stays in a folder on your computer and is there the next time you
open the app.

## What you need

- A Windows PC or a Mac, with an internet connection for the searches and PDF downloads.
- A Claude subscription (Pro or Max) and **Claude Desktop** (recommended) or **Claude
  Code**.
- About 300 MB of disk space for the app.

  Python is set up for you on first launch if you don't have a recent enough version.
  The launcher asks before it installs anything.

## Start it

1. Get the code:

   ```
   git clone https://github.com/ECONshare/literature-search-local.git
   ```

   Or download it as a ZIP and unpack it.
2. Open the folder it made, `literature-search-local`, and double-click the launcher:
   - **Windows:** `start-windows.bat`
   - **Mac:** `start-mac.command`. If macOS says it can't be opened, open **System
     Settings → Privacy & Security** and click **Open Anyway**, then try again. If you
     downloaded a ZIP, you can also run `bash start-mac.command` in Terminal from that
     folder.

The first start takes a few minutes while it installs what it needs. Later starts take
seconds. Your browser then opens LitReview at **http://127.0.0.1:8765**.

To stop LitReview, close its window, or choose **Quit LitReview** on the Settings page.

## Getting started

The first time, LitReview opens on **Getting started** (later it is in the menu at the
top). Each step has the button to press, and of steps 2 and 3, one is enough:

1. **Your email.** Unpaywall, which finds the free PDFs, needs a contact address.
2. **Connect Claude Code.** Open Claude Code in VS Code or Positron (or a terminal) and
   paste this in, once; it adds LitReview for every folder:

   ```
   claude mcp add --transport http --scope user litreview http://127.0.0.1:8765/mcp
   ```

   Then open a new Claude Code session: while LitReview is running, it is connected.
   **Test the connection** checks it.
3. **Connect Claude Desktop.** Press **Test the connection** first. If the connection
   already works, that is all. Otherwise quit Claude Desktop completely (from the menu bar
   or system tray, not just the window), press **Add LitReview to Claude Desktop**, open
   Claude Desktop again, and test once more. The test checks LitReview's side, then waits
   until Claude Desktop calls in; **Stop waiting** stops it. Claude Desktop lists LitReview
   as **LitReview Local**. When Claude asks whether it may use LitReview's tools, choose
   **Always allow**, so it can work through a queue without stopping to ask. Adding it by
   hand, and help when Claude does not connect, are on **Advanced setup**, linked there.
4. **Ask your first question.**

Claude in a web browser (claude.ai) can't connect to an app on your own computer. Use
Claude Desktop or Claude Code.

## How you work with it

1. In LitReview, create a project and write a research question.
2. The question then waits for Claude. In Claude, say:

   > **Work through my LitReview queue**

   Claude plans the search, ranks the results, reads each paper and writes the cited
   answer. LitReview does the searching, downloading and highlighting in between.
3. Read the answer in LitReview, edit it, and check any citation by hovering over it.
   **Ask Claude to check the citations** has Claude mark each one as supported, partial
   or unsupported.
4. Export the answer **with its evidence**, or the references as BibTeX, RIS or CSV.

Other things you can ask Claude:

- "Start a new question in my thesis project: …"
- "Draft 1,500 words from RQ2 with citations I can check."
- "Parse the PDFs in my project that have no text."
- "Tag the papers on remote work as 'Chapter 2'."

Reading papers uses your Claude allowance. A question with six papers takes Claude
roughly half an hour and several hundred thousand tokens, mostly for reading. On a Pro
plan, expect a few questions per usage window; Max is more comfortable. You choose how
many papers each question reads on the Settings page.

Claude Desktop holds a limited amount of text per chat, and papers are long. After a few
papers, Claude may suggest continuing in a new chat. Say the same sentence there: the
queue remembers where it was.

## Your data

Everything is kept in a folder called **LitReview** in your home folder: the library
(`litreview.db`), the PDFs, and a backup of the library from every start (the last 10 are
kept). To keep it somewhere else, start the app with `LITREVIEW_DATA_DIR` set to another
folder.

LitReview sends your search terms to the scholarly databases and downloads PDFs from
open-access sources. Claude sees what you ask it to work on: questions, abstracts and
paper texts. Nothing else leaves your computer.

## Updating

In the folder you cloned, run `git pull`, then start LitReview as usual. Your library is
kept; any changes to it are applied automatically at start.

## Troubleshooting

[docs/install.md](docs/install.md) has the full step-by-step guide, with more fixes.

- **The launcher says the port is in use.** Something else is using port 8765. Start
  LitReview on another port with `LITREVIEW_PORT=8780` before launching. Claude Desktop
  finds the new port by itself. For Claude Code, set the same variable.
- **Claude says it can't reach LitReview.**
  - Claude Desktop starts LitReview by itself when it needs it.
  - Claude Code needs LitReview to be running: double-click the launcher.
  - After adding LitReview to Claude Desktop, quit Claude Desktop completely once.
- **A PDF couldn't be downloaded.** Many papers aren't open access. Upload the PDF on the
  article's card and it is read for every question that holds it.
- **A paper has no text.** It is a scan. Choose **Ask Claude to parse** on its card, then
  ask Claude to work through the queue.
- Logs are in the `logs` folder inside your LitReview folder.

## For developers

The app is a FastAPI app in `litreview_local/`, backed by SQLite. Every step that needs
a model is a task in a queue (`tasks.py`) that the user's Claude works through via the MCP
server (`mcp_server.py`). `mcp_bridge.py` connects Claude Desktop to it, and `launcher.py`
sets up Python and starts the app. To run the tests (Python 3.10+):

```
pip install -r requirements-dev.txt
python -m pytest
```

## License

MIT. See [LICENSE](LICENSE).
