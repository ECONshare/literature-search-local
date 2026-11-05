# Installing and running LitReview on your own computer

LitReview Local runs on your own Windows PC or Mac. Your projects, articles,
PDFs, notes and answers stay in a folder on your computer. Your own Claude does
the reading and writing: Claude Desktop (the main way) or Claude Code, on your
own Claude subscription. LitReview never contacts an AI service itself.

This guide covers:

- [What you need](#what-you-need)
- [Getting LitReview](#getting-litreview)
- [Starting it](#starting-it)
- [Connecting Claude Desktop](#connecting-claude-desktop)
- [Connecting Claude Code](#connecting-claude-code)
- [Stopping it](#stopping-it)
- [Where your data lives](#where-your-data-lives)
- [Troubleshooting](#troubleshooting)

## What you need

- A computer running Windows 10 or 11, or macOS 12 or newer.
- A Claude subscription, with
  [Claude Desktop](https://claude.ai/download) or Claude Code installed.
- An internet connection. The first start downloads up to about 100 MB, and
  LitReview searches OpenAlex, Semantic Scholar and Crossref online. Everything
  else works offline.

You don't need to install Python yourself. LitReview checks for it the first
time and offers to set it up if it's missing (see [the first start](#the-first-start)).

> **claude.ai in a web browser can't connect to LitReview.** The website
> can't reach a program running on your own computer. Use the Claude Desktop
> app or Claude Code instead.

## Getting LitReview

The easiest way to get LitReview, and later to update it, is with
[GitHub Desktop](https://desktop.github.com/):

1. Install GitHub Desktop and open it.
2. Choose **File > Clone repository**, pick the LitReview repository, and
   choose a folder for it, such as your Documents folder.

If you use a terminal, this does the same:

```
git clone https://github.com/ECONshare/literature-search-local.git
```

Everything below happens in the folder you cloned, `literature-search-local`.

**Downloading a ZIP works too.** Use the green **Code** button on GitHub,
then **Download ZIP**, and unzip it. Two things differ when you start the app
from a ZIP: Windows and macOS warn you the first time (see
[Troubleshooting](#windows-protected-your-pc)), and on a Mac you may have to
start it from Terminal once.

**To update LitReview later**, choose **Fetch origin** and then **Pull** in
GitHub Desktop (or run `git pull`), then start LitReview as usual. If an update
needs new components, LitReview installs them by itself the next time it starts.

## Starting it

### Windows

Open the `literature-search-local` folder and double-click **`start-windows.bat`**.

A black window opens. After a few seconds LitReview opens in your web browser,
at <http://127.0.0.1:8765>.

### Mac

Open the `literature-search-local` folder in Finder and double-click **`start-mac.command`**.

A Terminal window opens. After a few seconds LitReview opens in your web
browser, at <http://127.0.0.1:8765>.

If nothing happens, or macOS says the file can't be opened, see
[the Mac troubleshooting below](#mac-start-maccommand-cant-be-opened-or-nothing-happens).

### Leave the window open

The window shows that LitReview is running, the address to open in your
browser, and where your data is kept. Leave it open while you use LitReview;
you can minimize it. When you start LitReview again while it's already running,
it just opens the browser again.

### The first start

The first start takes a few minutes. LitReview sets up a private copy of what
it needs in a folder called `.venv` inside the LitReview folder. Later starts
take a few seconds.

LitReview needs Python 3.10 or newer, and it first looks for one already on
your computer. If it finds none, it asks:

```
Install uv now? Type y and press Enter (or just Enter to cancel):
```

[uv](https://docs.astral.sh/uv/) is a small, free, widely used tool that
downloads a private copy of Python just for LitReview. Type `y` and press
Enter to let it set up Python for you. uv installs into your user folder and
needs no administrator rights. If you'd rather install Python yourself, press
Enter: LitReview then explains how to get Python from
[python.org](https://www.python.org/downloads/).

## Connecting Claude Desktop

This is the main way to use LitReview with Claude.

1. Start LitReview. The first time, it opens on **Getting started**; after
   that, Getting started is in the menu at the top. Step 3 there, **Connect
   Claude Desktop**, has the buttons.
2. Press **Test the connection** first. It checks LitReview's own side,
   that the program Claude Desktop starts for LitReview works, before
   anything is added to Claude Desktop. If Claude Desktop already uses
   LitReview, the test says the connection works, and you are done.
   Otherwise it says what to do next: steps 3 to 5.
3. **Quit Claude Desktop completely.** Closing its window is not enough,
   because Claude Desktop keeps running in the background, and it rewrites its
   settings file while it runs, which could undo the next step:
   - **Windows:** right-click the Claude icon by the clock (you may have to
     click the **^** arrow to see it) and choose **Quit**.
   - **Mac:** choose **Claude > Quit Claude** in the menu bar, or press
     **Cmd+Q**.
4. Press **Add LitReview to Claude Desktop**. It adds one entry, called
   **LitReview Local**, to Claude Desktop's settings (an entry an earlier
   version called `litreview` is renamed). Your other Claude Desktop settings
   are kept, and the old
   settings file is backed up next to it. **Advanced setup**, linked
   under the steps, shows the entry.
5. **Open Claude Desktop again**, and press **Test the connection** once
   more. LitReview checks that the entry is there and that the program Claude
   Desktop starts for it works, then waits for Claude Desktop to call in. If
   it asks you to, start a new chat and ask *"Use LitReview Local to list my
   projects"*. When Claude asks whether it may use LitReview's tools, choose
   **Always allow**, so that it can work through a queue without stopping to
   ask. The test turns green when Claude Desktop has called in; **Stop
   waiting** stops it asking.

From then on, Claude Desktop starts LitReview by itself when it isn't already
running, so you don't have to start it first. Started that way, LitReview has
no window (see [Stopping it](#stopping-it)).

### Adding it by hand

If you'd rather edit Claude Desktop's settings yourself, **Advanced setup** in
the app (linked under step 3 of Getting started) shows the text to add, with
these steps:

1. In Claude Desktop, open **Settings > Developer > Edit Config**. This opens
   the folder with `claude_desktop_config.json`.
2. Open that file in a text editor (Notepad or TextEdit).
3. Add the `LitReview Local` entry from Advanced setup inside `"mcpServers"`.
4. Save, then quit Claude Desktop completely and open it again.

The entry looks like this. The paths are specific to your computer, so copy
them from the page:

```json
{
  "mcpServers": {
    "LitReview Local": {
      "command": "C:\\Users\\You\\Documents\\literature-search-local\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\You\\Documents\\literature-search-local\\mcp_bridge.py"]
    }
  }
}
```

## Connecting Claude Code

Claude Code connects to LitReview directly, so **LitReview must be running**:
start it first (see [Starting it](#starting-it)).

Paste this into Claude Code once (in VS Code or Positron, say yes when it asks to
run it), or run it in a terminal:

```
claude mcp add --transport http --scope user litreview http://127.0.0.1:8765/mcp
```

Then open a new Claude Code session: while LitReview is running, it is connected.

`--scope user` makes LitReview available in every folder you use Claude Code
in. Without it, Claude Code adds LitReview only for the folder you ran the
command in.

To check that it worked, type `/mcp` in Claude Code: `litreview` should be
listed as connected. Step 2 of **Getting started** in the app shows the same
command, with the right port filled in, and its **Test the connection** button
says when Claude Code has called in.

If you open Claude Code inside the LitReview folder you cloned, you don't need
the command: Claude Code finds LitReview there by itself and asks you to
approve it the first time.

## Stopping it

You can stop LitReview at any moment. Everything is saved as you go, and it
picks up where it left off the next time it starts.

- **If you started it with `start-windows.bat` or `start-mac.command`:** press
  **Ctrl+C** in its window, or close the window. On Windows you may then be
  asked `Terminate batch job (Y/N)?`: type `Y` and press Enter.
- **If Claude Desktop started it:** it runs in the background without a window,
  and it keeps running after you quit Claude Desktop. To stop it, open
  <http://127.0.0.1:8765> and choose **Quit LitReview** on the Settings page.
  Restarting your computer stops it too.

**Quit LitReview** on the Settings page also works when you started it yourself.

## Where your data lives

LitReview keeps everything in one folder in your home folder, called
**`LitReview`**:

- **Windows:** `C:\Users\<your name>\LitReview`
- **Mac:** `/Users/<your name>/LitReview`

This folder is separate from the app's own folder, so you can update LitReview,
or even delete and re-download it, without touching your data. It contains:

| | |
|---|---|
| `litreview.db` | Your projects, questions, articles, screening decisions, tags, notes, highlights and answers |
| `pdfs/` | The PDFs, one folder per project |
| `backups/` | A copy of the database from each start (the last 10) |
| `settings.json` | What you set on the Settings page |
| `logs/` | What the app wrote while it ran; useful when something goes wrong |
| `server.json` | Present only while the app is running; it tells Claude where to find it |

**To back up your work,** stop LitReview, then copy the whole `LitReview`
folder somewhere safe.

**To keep your data somewhere else** (another drive, say), set the environment
variable `LITREVIEW_DATA_DIR` to that folder:

- **Windows:** open Command Prompt and run
  `setx LITREVIEW_DATA_DIR "D:\Research\LitReview"`, then close it.
- **Mac:** open Terminal and run
  `echo 'export LITREVIEW_DATA_DIR="$HOME/Research/LitReview"' >> ~/.zshrc`,
  then open a new Terminal window.

Then start LitReview again. If you have connected Claude Desktop, press **Add
LitReview to Claude Desktop** on Getting started again, so that Claude Desktop
uses the new folder too.

## Troubleshooting

### "Windows protected your PC"

Windows may warn you the first time you start `start-windows.bat` from a
downloaded ZIP:

- If it says **Windows protected your PC**, click **More info**, then
  **Run anyway**.
- If it says **Open File - Security Warning**, click **Run**.

Neither appears for a folder cloned with GitHub Desktop or git.

### Mac: start-mac.command can't be opened, or nothing happens

A ZIP download can lose the file's permission to run, and macOS can refuse
files it hasn't checked. Start it from Terminal once:

1. Open **Terminal** (in Applications > Utilities).
2. Type `bash` followed by a space.
3. Drag `start-mac.command` from Finder into the Terminal window, and press
   Return.

If macOS says Apple can't check the file for malicious software, open
**System Settings > Privacy & Security**, scroll down, and click
**Open Anyway** next to `start-mac.command`.

### Mac: a box offers to install "command line developer tools"

LitReview doesn't need them. You can click **Not Now**.

### "Python was not found; run without arguments to install from the Microsoft Store"

You may see this if you type `python` in a terminal. It is a shortcut Windows
puts there, not Python itself. LitReview ignores it, so you don't need to do
anything.

### The first start stops while "Installing the components LitReview needs"

This step downloads what LitReview needs, and the message underneath says what
went wrong. The usual causes:

- **No internet connection.** Connect, then start LitReview again.
- **A proxy** (common at universities and companies). Set the environment
  variable `HTTPS_PROXY` to your proxy's address, for example
  `http://proxy.example.org:8080`; your IT department knows it. Set it the same
  way as `LITREVIEW_DATA_DIR` [above](#where-your-data-lives).
- **A network that inspects secure connections** (the message mentions
  certificates). Try another network once, such as a phone hotspot. After the
  first start, LitReview needs the download only after updates.

### "Port 8765 is already in use" or "doesn't let programs use port 8765"

Another program is using the address LitReview wants, or Windows has reserved
it. Give LitReview another port by setting `LITREVIEW_PORT` to, for example,
`8766`:

- **Windows:** open Command Prompt, run `setx LITREVIEW_PORT 8766`, close it,
  and double-click `start-windows.bat` again.
- **Mac:** run `echo 'export LITREVIEW_PORT=8766' >> ~/.zshrc` in Terminal, then
  double-click `start-mac.command` again.

Then press **Add LitReview to Claude Desktop** on Getting started again, so
that Claude Desktop uses the new port. Claude Code needs the command from there
again too, unless you start it with `LITREVIEW_PORT` set.

### Claude Desktop doesn't show LitReview

1. Make sure you **quit Claude Desktop completely** and opened it again (see
   [step 3 above](#connecting-claude-desktop)).
2. Press **Test the connection** on Getting started. It says what is wrong and
   what to do about it.
3. Look at Claude Desktop's log for LitReview:
   - **Windows:** `%LOCALAPPDATA%\Claude\logs\mcp-server-LitReview Local.log` (the Microsoft Store version) or `%APPDATA%\Claude\logs\mcp-server-LitReview Local.log`; Advanced setup shows the one it finds (paste that
     into the File Explorer address bar)
   - **Mac:** `~/Library/Logs/Claude/mcp-server-LitReview Local.log` (in Finder, choose
     **Go > Go to Folder** and paste it)

### Claude says LitReview isn't running and couldn't be started

Start LitReview yourself by double-clicking the launcher, and wait until it
opens in your browser. Then ask Claude again. What went wrong is in
`logs/server-bridge.log` in your [data folder](#where-your-data-lives).

### Claude Code says it can't connect to litreview

Claude Code doesn't start LitReview for you. Start LitReview first, then type
`/mcp` in Claude Code to reconnect. `claude mcp list` shows what Claude Code
is set up to use.

### Starting over with a fresh setup

If LitReview's setup gets into a bad state, for example after an interrupted
first start, delete the `.venv` folder inside the LitReview folder, and start LitReview
again. It sets everything up anew. Your data isn't in that folder, so nothing
is lost.

On a Mac, files whose names start with a dot are hidden in Finder. Press
**Cmd+Shift+.** to show them.

### Antivirus warnings

Some antivirus programs are wary of new programs that start other programs.
If yours stops `python.exe`, `uv.exe` or the launcher, allow them for the
LitReview folder.
