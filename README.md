# Kimi Custom Commands

A standalone [Kimi](https://github.com/webhead2oo9/kimi-agent) module for guild-scoped custom
slash commands. Staff edit commands through ephemeral Discord controls and modals; members invoke
the resulting commands normally. Content is stored in module-owned SQLite tables and command
registrations update without restarting the bot.

## Requirements

- Python 3.14+
- A Kimi host that provides the required capabilities below
- `kimi-agent-module-api>=1.2,<2`, provided by Kimi's environment
- Host capabilities `discord.guild_commands.v1`, `discord.modals.v1`, and
  `discord.components_v2.v1`

The package does not use Kimi's raw bot or raw database ports.

## Install and activate

Clone this repository next to Kimi and check out the reviewed tag or exact commit you want to
run:

```console
git clone https://github.com/webhead2oo9/kimi-agent-custom-commands.git /path/to/kimi-agent-custom-commands
git -C /path/to/kimi-agent-custom-commands checkout --detach <reviewed-tag-or-commit>
```

Then run this from Kimi's `bot/` directory. `--no-deps` keeps Kimi's reviewed module API and other
host dependencies authoritative:

```console
uv pip install --python .venv/bin/python --no-deps --editable /path/to/kimi-agent-custom-commands
```

Then add the module entry-point name to the comma-separated `KIMI_MODULES` value in Kimi's
dotenv, preserving any modules already enabled:

```dotenv
KIMI_MODULES=custom_commands
```

For example, `KIMI_MODULES=moderation` becomes
`KIMI_MODULES=moderation,custom_commands`. The module has no guild settings file and requires no
privileged gateway intents. The bot needs the ordinary permission to use application commands and
to send messages in channels where staff ask the AI to create a review card.

Restart Kimi after installing and enabling the module. Then verify that:

1. Startup logs show `Kimi module started: custom_commands <version>`.
2. `/modules status` reports `custom_commands` as healthy in the target server.
3. Staff can run `/custom-command create`, while members cannot use the management group.
4. A saved test command appears and can be invoked by a member without another restart.

A later `uv sync` of the Kimi checkout may remove an independently installed module; if so,
install it again.

## Guild scope and access

`KIMI_MODULES=custom_commands` loads the module into the Kimi process; it does not select one
server. The module follows Kimi's active-guild configuration. A guild must be approved through
`ALLOWED_GUILD_IDS` or through `<CONFIG_DIR>/servers/<guild_id>.md` with `bot_active: true`. An
explicit `bot_active: false` disables the bot there. The module has no separate
`guild-modules/<guild_id>/custom_commands.md` settings file, so it is available independently in
every guild where Kimi is active.

The module never accepts a guild ID from a user or the model. It derives the scope from the trusted
Discord interaction or server conversation:

| Surface | Guild used | Who can use it |
|---|---|---|
| `/custom-command create`, `edit`, `delete`, and `list` | The guild where the slash command was invoked | Kimi staff only |
| AI read/propose tools | The current server conversation's guild | Kimi staff only |
| Proposal previews, Approve, and Reject | The proposal's guild; cross-guild controls are rejected | Any current Kimi staff member in that guild, including the proposer |
| A generated `/name` command | Only the guild in which it was saved | Any member of that active guild |

`/custom-command` is synchronized as a global management group so it exists before a server has
any generated commands. Its handlers still reject DMs, inactive guilds, and non-staff users.
Discord may therefore show the group to a non-staff member, but attempting to use it returns an
ephemeral `Staff only.` response. Discord's own integration command permissions may hide or further
restrict commands, but cannot bypass Kimi's runtime trust check.
Generated commands are registered with Discord only for their owning guild. Their records,
editor sessions, proposals, autocomplete, and command synchronization are also keyed by that guild,
so two guilds may define the same command name without sharing content. An editor session is
additionally bound to the staff member who opened it; another staff member cannot take it over.

"Staff" means Kimi's resolved `staff` trust tier, not Discord Administrator permission by itself.
Configure it globally with `STAFF_USER_IDS` or `STAFF_ROLE_IDS`, or add guild-local staff in the
server file:

```yaml
---
bot_active: true
staff_user_ids: [123456789012345678]
staff_role_ids: [234567890123456789]
---
```

Guild-local lists are additive to the global lists. Guild-local trust-list changes are read on the
next interaction; changes to global dotenv settings require a restart. `OWNER_USER_ID` alone does
not grant the guild `staff` tier.

## Staff commands

- `/custom-command create name description` opens a new draft.
- `/custom-command edit name` opens an existing command, with autocomplete.
- `/custom-command delete name` deletes and republishes the guild command set.
- `/custom-command list` shows names and revisions.

The editor supports pages and heading, text, field, divider, image-gallery, and small-text blocks.
Pages and blocks can be selected, added, edited, reordered, and deleted. Save uses optimistic
revision checking; Discard reloads the stored command; Close drops the in-memory draft. Sessions
expire after 30 minutes.

Every generated command has an optional `hidden` argument. The default response is public;
`hidden:true` makes it ephemeral. Multi-page responses include a page selector. Mentions are
disabled by the module interaction adapter.

## AI proposals

Two staff-only searchable tools are registered:

- `custom_commands_read` searches or reads stored definitions.
- `custom_commands_propose` proposes a complete create, replace/rename, or delete operation.

AI proposals never save directly. The module posts an Approve/Reject review card in the invoking
channel. Approval atomically compares the recorded revision, changes the command, and decides the
proposal before publishing Discord registrations. The requesting staff member may approve their
own proposal. Staff can preview both the current and proposed rich output, including every page,
before deciding.

## Data and privacy

The module does not subscribe to message or member events, read channel history, call external
HTTP services, or send custom-command content to the model on its own. It receives slash-command,
button, select, and modal values only when someone uses its controls. Image and thumbnail URLs are
stored as text and rendered by Discord; the module does not download them.

It owns two tables in Kimi's shared SQLite database:

| Table | Stored data | Retention |
|---|---|---|
| `custom_commands_commands` | Server ID, stable command ID, name, description, versioned page/block JSON, revision, creator/updater user IDs, and timestamps | Until staff delete the command or an operator deletes the module data |
| `custom_commands_proposals` | Server and proposal IDs, target command/revision, proposed content, state, proposer/decider user IDs, summary, decision reason, and timestamps | Retained as a review history until an operator deletes the module data |

Editor drafts exist only in bot memory and expire after 30 minutes. Restarting the bot discards
open drafts but not saved commands or proposals. Removing the package does not automatically delete
its database tables.

The module declares only the `send_message` Discord action, used to post AI proposal review cards.
It declares no event subscriptions or outbound network hosts.

## Development

```shell
uv sync --locked --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python -m pytest -q
uv build --no-sources
```

The checked-in lock file resolves a compatible module API from PyPI. CI requires that lock so
tests, builds, and dependency audits use the same dependency set as local development.

## Versioning

This module is distributed from this Git repository, not PyPI. Releases are Git tags whose version
matches `pyproject.toml`. CI tests the module and validates its wheel and source distribution, but
does not publish them. Operators should review a tag or exact commit, check it out, and reinstall
the editable package when changing versions.

## License

MIT
