# claude-agents: long-lived Claude Code agents, one per directory

A small supervisor, run by systemd, that keeps one Claude Code agent running
for every directory under `AGENTS_ROOT`. Each agent is:

* an interactive `claude --remote-control NAME` session, so claude.ai and the
  Claude app list it and can open it;
* listening on the **XMPP channel**, so messages are pushed in as they arrive;
* in its own window of the supervisor's tmux server, for local access over
  ssh: `claude-agents attach NAME` (detach with `C-b d`);
* resumed into the same conversation whenever it starts: after a crash, a
  reboot, or Remote Control giving up during a network outage.

Why not `claude remote-control --spawn …`? That server starts sessions as
headless children with a fixed command line, and rejects `--channels`
("Unknown argument", Claude Code 2.1.281): its sessions can use xmpp-mcp's
tools, but nothing is ever pushed to them. The interactive form takes
`--channels` like any interactive session; it needs a terminal, which tmux
provides.

## Nothing to record per agent

* **An agent is a directory** in `AGENTS_ROOT`, or a symlink there to a
  directory anywhere (a repository worktree, say). Its name is the entry's
  name. Create one and the supervisor starts it within seconds; remove it and
  the agent is stopped.
* **Its conversation is found, not configured**: the one the supervisor last
  ran there, or else the newest Claude Code keeps for that directory
  (conversations are filed by directory). An empty directory starts a new
  conversation.
* **It appears in the app by itself.** Each session registers with claude.ai
  when it starts. On a restart it rejoins the same claude.ai session.
* **Its XMPP identity follows too.** With `XMPP_JID={session}.{host}@…` the
  address comes from the conversation, which resuming keeps. The friendly
  name is the agent's name.

The only state is the supervisor's own, in `~/.local/state/claude-agents/`:
the conversation each agent last ran, and a fork waiting to start.

## Commands

```
claude-agents list                        agents, running or not, session IDs, claude.ai links
claude-agents attach NAME                 its terminal (local-only commands, debugging)
claude-agents new NAME [--dir PATH]       add an agent (with --dir: a symlink to PATH)
claude-agents fork PARENT CHILD [--dir PATH]
claude-agents restart NAME                clean stop; the supervisor resumes it
```

**Forking.** `fork` makes CHILD's directory (a new git worktree of the
parent's repository on a branch named CHILD, if the parent's directory is
in one, else a plain directory, or `--dir`). It files a copy of the parent's
conversation under the child's directory, because `--resume` finds
conversations by directory. The child then starts with
`--resume <parent> --fork-session`: the parent's history, but a session of
its own, so its own claude.ai session and its own XMPP address. The parent
carries on untouched. An agent can fork itself by running the command.

## Setup (once per machine)

1. **xmpp-mcp** and the **plugin**, with the channel **approved** in
   managed settings. See "Plugin" in `docs/CHANNELS.md`: `uv tool install`,
   `claude plugin marketplace add`, `claude plugin install xmpp@xmpp-mcp`,
   and `allowedChannelPlugins` in `/etc/claude-code/managed-settings.json`.
   The supervisor enables the plugin for its agents only.
2. **The host key**, mode 0600 (see `contrib/prosody/deploy/README.md`).
3. `claude-agents` into `~/.local/bin/`, `config.example.env` to
   `~/.config/claude-agents/config.env` (then edit), `claude-agents.service`
   to `/etc/systemd/system/`.
4. **Trust `AGENTS_ROOT` once**: run `claude` there and accept the
   workspace-trust prompt. Directories created inside it are trusted from then
   on. A symlinked agent runs in its target, which needs trusting (or a
   trusted parent) in the same way.
5. `systemctl enable --now claude-agents`.

## Moving an existing session in

A session started elsewhere, the `remote-control` server included, moves
in with its conversation. Its conversation is filed under its directory, so:

1. Stop it where it runs now. One conversation must not run in two
   processes.
2. `claude-agents new NAME --dir <its directory>`. The supervisor resumes the
   newest conversation there. If there could be several, first pin the right
   one in `~/.local/state/claude-agents/agents/NAME.json` as
   `{"session": "<its session ID>"}`.

It comes back as a new claude.ai session with the same conversation. Use that
one from then on; the old entry in the app belongs to the old process.

## Tested (a test VM, Claude Code 2.1.281)

Adding an agent by `mkdir` (it registered and appeared with its claude.ai
link); XMPP pushes to it; a fork that remembered the parent's conversation
but had its own session, claude.ai session and XMPP address;
`systemctl restart` (both agents resumed their conversations); `kill -9` of
one agent's Claude Code (restarted 10 s later, same conversation); removing
a directory (that agent stopped).
