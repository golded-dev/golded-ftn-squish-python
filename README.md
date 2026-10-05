# golded-ftn-squish

Repository: [`golded-ftn-squish-python`](https://github.com/golded-dev/golded-ftn-squish-python).
The distribution remains `golded-ftn-squish`; imports use `golded_ftn_squish`.
The source is public on GitHub. This package has not been released on PyPI.

Strict reader and offline writer for classic Squish `.SQD` and `.SQI` message areas. Python 3.12+;
plain Python, using the models and text helpers from `golded-ftn`.
Version 1.2.0 is prepared locally; these writer changes are unreleased.

```python
from pathlib import Path
from golded_ftn_squish import SquishReader

base = Path("messages/general")  # basename, without an extension
for message in SquishReader().read(base):
    print(message.msgno, message.from_name, message.subject)
```

For a local checkout:

```sh
git clone https://github.com/golded-dev/golded-ftn-python.git
git clone https://github.com/golded-dev/golded-ftn-squish-python.git
cd golded-ftn-squish-python
uv sync --locked
```

uv uses the sibling `../golded-ftn-python` repository. Wheels and source
distributions declare only `golded-ftn>=1.2.0,<2`; the development source override
and lock file are excluded from the sdist.

## Reading an area

`SquishReader.read(path, options=None)` implements `MessageBaseReader`. Pass an
area basename and optionally core `ReaderOptions(fallback_charset="CP850")`.
Extensions are matched case-insensitively; ambiguous candidates fail. Both files
must be regular files. Missing files and wrong file types raise filesystem errors.
Malformed records and decoding failures raise `ParserException` with the actual
file path, byte offset and chained cause. All active records are validated before
the result is returned; a damaged later record never produces a partial result.

Read a stable area without concurrent writes. The two files are read separately,
without locking: this does not create an atomic snapshot.

The supported layout has a 256-byte area header, 28-byte frame header, 238-byte
message header and 12-byte index records. Extended layouts, compressed frames and
frames being updated are rejected. The area header's message count selects the
active index prefix; preallocated index tail records are ignored. Active records
must have increasing, positive unique UIDs and distinct, non-overlapping frames.
UID gaps and arbitrary unused allocation bytes within a frame are accepted.
Unindexed old and free frames are ignored; an active record pointing to a free
frame is an error. `.SQL` lastread data is not used.

## Message mapping

- `msgno` is the persistent index UID, not its relative index position. Header
  UID is checked against it only when the `MSGUID` attribute marks it as valid.
- Names, subject and body decode strictly through core helpers. Charset controls
  in the separate control block precede body declarations. Conflicting
  declarations fail; otherwise core charset aliases and CP850 fallback apply.
  Mojibake repair is left to callers.
- `body_text` contains only the normalized body payload. Separate header controls
  are preserved in `control_lines`, before body controls. Controls are separated
  by SOH bytes. A final NUL is optional inside the declared length; after the first
  NUL only NUL padding is accepted. Empty controls, trailing SOH and line breaks in
  the control block are rejected. Repeated controls remain in source order.
- Header and body MSGID/REPLY values must agree; identical repetitions are
  accepted. `external_id` is MSGID, otherwise a core synthetic ID over decoded,
  normalized fields. Routing metadata comes from the combined control parser.
- Binary addresses preserve node 0 and nonzero points. Incomplete components may
  be supplemented by INTL/FMPT/TOPT, following the MSG package's conflict rules.
  Unresolvable addresses become `None`; binary Squish addresses have no domain.
- Valid packed `DateWritten` is timezone-aware UTC. The signed writer UTC offset
  does not change it. A zero packed date permits a valid FTSC-date fallback, which
  is timezone-naive. Invalid dates become `None`, without calendar normalization.
  Binary UTC offset and arrival time have no dedicated core fields; existing
  TZUTC controls remain available as metadata.
- Attributes remain raw. `reply_to_msgno` and `reply1st_msgno` preserve header
  UIDs, with zero mapped to `None`. `reply_next_msgno` is `None`: the remaining
  eight reply slots form a reply list, not a next-sibling pointer. The core model
  cannot represent that full list.
- Provenance records source type `squish`, the actual SQD path, UID as text and
  frame byte offset. Area metadata and other unknown fields remain `None`.

Area discovery, packing, repair and databases are outside this package.

## Development

Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy` and `uv run python -m mypy.stubtest golded_ftn_squish`.
Build with `uv build`; then run `uv run twine check dist/*` and
`uv run python scripts/verify_distribution.py`. The distribution check rebuilds
from sdist and tests both wheels with a local core wheel in separate environments
outside the repositories. CI runs these checks on Linux Python 3.12–3.14 and tests
on Windows/macOS Python 3.14. Local results do not establish a passing CI run.

## Format references

The binary layout is documented in the original Maximus
[Squish structures](https://github.com/fidosoft/maximus/blob/master/msgapi/api_sq.h)
and [MsgAPI headers](https://github.com/fidosoft/maximus/blob/master/msgapi/msgapi.h).
[Index allocation](https://github.com/fidosoft/maximus/blob/master/msgapi/sq_idx.c)
explains the unused tail. GoldED's `gmosqsh.h` and its reader/writer provide a
second implementation reference. The PHP `laravel-ftn-squish` reader and tests
provide compatibility context; its silent truncation and corruption handling are
not copied. Tests use independent synthetic bytes, never private message archives.

MIT license. See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and
[release instructions](docs/release.md).

## Archive mode

Strict reading remains the default. Archive mode requires a report callback:

```python
from golded_ftn import ReaderIssue, ReaderOptions

issues: list[ReaderIssue] = []
options = ReaderOptions(archive_mode=True, on_issue=issues.append)
# Pass options to SquishReader().read(source, options).
```

Issues carry `recovered`, `skipped` or `stopped`, the actual filename, record
identity and physical offset. Their detail contains no message contents. A stop
means the traversal is incomplete; a validated prefix may still be returned.
Multiple issues can describe one record, including recovery followed by a skip.
Filesystem errors and callback exceptions propagate. Files must remain stable.

The index UID wins over a disagreeing header UID and empty SOH control segments
are ignored, both with reports. Failed indexed frames or messages are skipped
using the next index slot. Duplicate offsets, overlapping frame extents, invalid
UID order and truncated indices stop traversal. Metadata conflicts are skipped.

If declared ASCII cannot decode a payload, the configured fallback is tried
strictly and reported. The original charset control stays unchanged. Other
decoding failures are skipped; there is no lossy decoding or mojibake repair.


## Writing an area

`SquishWriter.create(path)` creates `.SQD`, `.SQI` and an empty `.SQL` lastread
file. Existing files are rejected. The base header stores the basename, with
no extension, in its 80-byte name field. The first new UID is 2, following
GoldED's initialization. Existing `.SQL` bytes remain untouched by editing.

```python
from pathlib import Path

from golded_ftn import MessagePatch, OutgoingMessage
from golded_ftn_squish import SquishWriter

writer = SquishWriter()
Path("messages").mkdir(exist_ok=True)
writer.create("messages/new-area")
with writer.open("messages/new-area") as session:
    created = session.append(
        OutgoingMessage(
            from_name="Alice",
            to_name="Bob",
            subject="Hello",
            body_text="Body",
        )
    )
    current = session.read(created.identity.msgno)
    changed = session.update(
        current.identity, MessagePatch(subject="Revised"), current.revision
    )
    session.delete(changed.identity, changed.revision)
```

Sessions acquire byte 0 of `.SQD` per operation, reread the whole base and validate
the physical frames, both linked lists and the active index before writing.
Revision tokens include the identity, frame offset and raw message bytes;
neighbor frame links are excluded because an unrelated append changes them.
A changed or missing target raises `ConflictError`. Unrelated message changes do
not invalidate the token. Omitted patch fields retain their values; explicit
`None` clears only fields the format can represent as absent.

Content changes append a new frame and move the old frame to the free list.
UIDs stay fixed. Free frames are kept for other tools; this writer does not
recycle, merge or pack them. Header patches preserve omitted raw metadata, all
nine replies, arrival time, UTC offset and unknown attribute bits. Attribute-only
changes preserve the original control and text bytes. A body-only patch preserves
omitted SEEN-BY and PATH routing. Explicit control
replacement replaces the control block while preserving an omitted `external_id`;
conflicting MSGID controls are rejected. New names and subjects require room for
their trailing
NUL; oversized fields and unencodable text fail instead of being truncated.
`WriterOptions` selects the strict encoding, CP850 by default. Charset declarations
must agree when text is serialized. Routing and MSGID are explicit caller data.

One operation is the rollback unit. Injected write, truncate and flush failures
restore watched files in place under the lock. A failed rollback raises
`RollbackError` and makes the session unusable. This does not promise recovery
from process termination or power loss. A configured `maxmsgs` limit rejects an
append at capacity; editing never purges other messages automatically.

Use Squish offline with GoldED closed. `concurrent=True` is rejected on every
platform. macOS is the only runtime tested in this checkout. POSIX record locks
serialize cooperating Python writers; Linux execution remains unverified. Windows
has
a byte-lock implementation but has not been exercised here. GoldED build tests,
concurrent reading and refresh checks are deferred, so no GoldED build is certified.
See [writer source notes](docs/writer-sources.md).
