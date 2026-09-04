# Kimi Custom Commands

This module lets the staff of a Discord server create their own slash commands
for [Kimi](https://github.com/webhead2oo9/kimi-agent) without writing code. Staff
build a command in a small editor inside Discord. Members then use it like any
other slash command. New commands appear without restarting the bot.

Each server's commands are private to that server. Two servers can use the same
command name without seeing each other's content.

## What staff and members get

Staff use the `/custom-command` group:

- **create** starts a new command from a name and description.
- **edit** opens an existing command. Names autocomplete.
- **delete** removes a command.
- **list** shows every command and its revision number.

The editor works in pages and blocks. A block can be a heading, paragraph, field,
divider, image gallery, or small text. Blocks and pages can be added, edited,
reordered, and deleted. Save refuses to overwrite a version someone else saved in the
meantime. Discard reloads the saved version. An open editor times out after 30
minutes of inactivity, and only the staff member who opened it can use it.

Members run a saved command as `/name`. The response is public unless they add
`hidden:true`, which shows it only to them. Commands with several pages include a
page selector. Responses never ping anyone.

Kimi's AI can also read commands and suggest changes when a staff member asks it
to. A suggestion is never saved on its own. The module posts an Approve/Reject card
in the channel, with a preview of the current and proposed versions. Any staff
member in that server can decide, including the person who asked for it.

## Before you install

You need:

- A running Kimi with Python 3.14 or newer.
- A Kimi version that provides module API version 2, guild commands, modals, and
  components v2. If it does not, the module stays off and says so in
  `/modules status`.
- The bot's ordinary permission to use application commands, plus permission to
  send messages in channels where staff ask the AI for a proposal.

No privileged intents are required.

## Install

1. Clone this repository next to Kimi and check out the tag or commit you want to
   run:

   ```console
   git clone https://github.com/webhead2oo9/kimi-agent-custom-commands.git /path/to/kimi-agent-custom-commands
   git -C /path/to/kimi-agent-custom-commands checkout --detach <tag-or-commit>
   ```

2. From Kimi's `bot/` directory, install the module into Kimi's own Python
   environment. The `--no-deps` flag keeps Kimi's already-installed dependencies
   in charge:

   ```console
   uv pip install --python .venv/bin/python --no-deps --editable /path/to/kimi-agent-custom-commands
   ```

3. Add `custom_commands` to the `KIMI_MODULES` line in Kimi's environment file.
   Keep whatever is already there, separated by commas:

   ```dotenv
   KIMI_MODULES=moderation,custom_commands
   ```

4. Restart Kimi.

Then confirm it is working:

- The startup log contains `Kimi module started: custom_commands <version>`.
- `/modules status` shows `custom_commands` as healthy in the server.
- A staff member can run `/custom-command create`. A regular member gets
  "Staff only."
- A saved test command can be run by a member straight away.

If you later run `uv sync` in the Kimi checkout, it may uninstall this module. Run
the install command again if that happens.

## Which servers get it

The module is available in every server where Kimi itself is active. There is no
separate per-server settings file for it. A server is active when it is listed in
`ALLOWED_GUILD_IDS`, or when its server file at
`<CONFIG_DIR>/servers/<guild_id>.md` says `bot_active: true`.

The module always works out which server it is in from the Discord interaction
itself. It never accepts a server ID typed by a user or suggested by the AI.

| Action | Who can do it |
|---|---|
| `/custom-command` create, edit, delete, list | Kimi staff in that server |
| Ask the AI to read or propose commands | Kimi staff in that server |
| Approve or reject a proposal | Any Kimi staff member in that server |
| Run a saved `/name` command | Any member of that server |

Discord may show the `/custom-command` group to everyone, because it is registered
globally so it exists before a server has any commands. Non-staff users who try it
get a private "Staff only." reply. Discord's own command permission settings can
hide it further but cannot grant access.

## Who counts as staff

"Staff" means Kimi's staff trust tier, not the Discord Administrator permission.
Set it globally with `STAFF_USER_IDS` or `STAFF_ROLE_IDS` in the environment file,
or add staff for one server at the top of its server file:

```yaml
---
bot_active: true
staff_user_ids: [123456789012345678]
staff_role_ids: [234567890123456789]
---
```

Server-file lists add to the global lists. Changes to a server file are picked up
on the next interaction. Changes to the environment file need a restart. Being the
`OWNER_USER_ID` does not by itself make someone staff in a server.

## Data and privacy

The module does not read messages, watch members, look at channel history, call
any outside service, or send command content to the AI unless a staff member asks.
It only receives the values people type into its own controls. Image and thumbnail
URLs are stored as text and shown by Discord. The module never downloads them.

It keeps two tables in Kimi's shared database:

| Table | What it holds | How long |
|---|---|---|
| `custom_commands_commands` | Server ID, command ID, name, description, page and block content, revision, who created and last edited it, timestamps | Until staff delete the command or an operator removes the table |
| `custom_commands_proposals` | Server and proposal IDs, target command, proposed content, state, who proposed and decided it, summary, reason, timestamps | Kept as history until an operator removes the table |

Open editors live only in memory. Restarting Kimi discards unsaved edits but not
saved commands or proposals. Uninstalling the module does not delete its tables.

The only Discord action the module uses is sending a message, for the proposal
card. It does not subscribe to any events or talk to any outside hosts.

## Updating

Releases are Git tags. There is no PyPI package. To move to a new version, check
out the new tag in your clone and run the install command from step 2 again. The
tag's version always matches the one in `pyproject.toml`.

## For developers

You only need this if you are changing the module's code.

```shell
uv sync --locked --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python -m pytest -q
uv build --no-sources
```

The lock file pulls `kimi-agent-module-api` from PyPI so tests run without a Kimi
checkout. CI runs the same commands and checks the built package but does not
publish it.

## License

MIT
