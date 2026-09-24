# Long-lived Claude Code agents on XMPP, under systemd

Each agent is its own interactive Claude Code session with **Remote Control**
(reachable from claude.ai and the Claude app) and the **XMPP channel**
(messages pushed in as they arrive), kept running by systemd and resuming
the same conversation every time it starts.

Why not `claude remote-control --spawn …`? That server starts each session
as a headless child with a fixed command line, and nothing can add
`--channels` to it: its sessions can use xmpp-mcp's tools but never receive
pushes. The interactive form, `claude --remote-control NAME`, takes the same
flags as any interactive session. It needs a terminal, which tmux provides.

## Pieces

| File | Goes to | |
|---|---|---|
| `claude-agent-run` | `~/.local/bin/` | starts one agent: `--remote-control NAME --name NAME --channels plugin:xmpp@xmpp-mcp`, plus `--resume` with its recorded session ID |
| `claude-agent@.service` | `/etc/systemd/system/` | one unit per agent, `claude-agent@NAME`, running that in its own tmux server |
| `example.env` | `~/.config/claude-agents/NAME.env` | the agent's directory and xmpp-mcp settings |

The session ID lives in `~/.local/state/claude-agents/NAME.session`. It is
recorded once the conversation exists, and reused by `--resume` on every
start. Resuming keeps the ID, so the claude.ai session and the agent's XMPP
address stay the same across restarts. Delete the file to start the agent
afresh.

## One-off setup (per machine)

1. **xmpp-mcp** on the path: `uv tool install "xmpp-mcp[webhook] @ git+https://github.com/abligh/xmpp-mcp@xmpp-channels"`.
2. **The plugin**, from a checkout of this repository:

   ```bash
   git clone --branch xmpp-channels https://github.com/abligh/xmpp-mcp.git ~/.local/share/xmpp-mcp-src
   claude plugin marketplace add ~/.local/share/xmpp-mcp-src
   claude plugin install xmpp@xmpp-mcp --scope local   # run in any scratch directory
   ```

   It is enabled per agent by the launcher (`--settings`), not for every
   session on the machine.
3. **Approve the channel**, so agents start without the development-channels
   confirmation (which an unattended restart cannot answer). In
   `/etc/claude-code/managed-settings.json`:

   ```json
   {
     "channelsEnabled": true,
     "allowedChannelPlugins": [ { "plugin": "xmpp", "marketplace": "xmpp-mcp" } ]
   }
   ```

   This list replaces Claude Code's built-in list of approved channel
   plugins, so add any official ones you also use.
4. **The host key** for this machine (see `contrib/prosody/deploy/README.md`),
   mode 0600.
5. Install the launcher and unit, then `systemctl daemon-reload`.

## Adding an agent

```bash
cp example.env ~/.config/claude-agents/reviewer.env     # then edit
cd /path/to/its/dir && claude                           # accept workspace trust once, then exit
sudo systemctl enable --now claude-agent@reviewer
tmux -L agent-reviewer attach                           # look at it; detach with C-b d
```

## Moving an existing agent in, with its conversation

Agents currently running under `claude remote-control --spawn worktree` keep
their conversation if resumed by ID in the same directory. This is the route
measured in an earlier migration experiment, and the one tested here.

1. Find the session: `~/.claude/sessions/*.json` has `sessionId` and `cwd`
   for every live session (the worktree it runs in).
2. **Stop it under the Remote Control server first.** One conversation must
   not run in two processes.
3. `AGENT_DIR=<that cwd>` in the env file, and the ID in
   `~/.local/state/claude-agents/NAME.session`.
4. `systemctl enable --now claude-agent@NAME`. It resumes the conversation,
   now with the XMPP channel. The directory has to be trusted (step 2 of
   "Adding an agent" above); `--resume` looks the transcript up by the
   directory.

## What was tested (a test VM, Claude Code 2.1.281)

* Plugin not approved: the channel is refused ("not on the approved channels
  allowlist"). Approved in managed settings: no prompt, and messages pushed.
* `systemctl restart`, and a `kill -9` of Claude Code (systemd restarts it
  10 s later): each time the same session ID, the same claude.ai session,
  channel delivery working, and the conversation intact (the agent could
  list every message it had been sent).
* The only prompt is workspace trust, the first time in a new directory.
