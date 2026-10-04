# golded-ftn-squish

Repository: [`golded-ftn-squish-python`](https://github.com/golded-dev/golded-ftn-squish-python).
The distribution remains `golded-ftn-squish`; imports use `golded_ftn_squish`.
The source is public on GitHub. This package has not been released on PyPI.

Strict reader for classic Squish `.SQD` and `.SQI` message areas. Python 3.12+;
plain Python, using the models and text helpers from `golded-ftn`.

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
distributions declare only `golded-ftn>=1.1.0,<2`; the development source override
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

This package reads messages. Writers, area discovery, repairs and databases are
outside version 1.0.0.

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
