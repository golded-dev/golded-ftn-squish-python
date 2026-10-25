# F-Squish Handoff Report: `golded-ftn-squish` `1.3.0`

## Scope and isolation
- **Worktree:** `/Users/odinn/Projects/golded-dev/ftn-node-work/worktrees/golded-ftn-squish-python-f1/`
- **Engine:** `/Users/odinn/Projects/golded-dev/ftn-node-work/worktrees/golded-ftn-python-r1/` (`golded-ftn` 1.3.0, editable path)
- **Untouched:** `/Users/odinn/Projects/golded-dev/golded-ftn-squish-python/` and `/Users/odinn/Projects/golded-dev/golded-ftn-python/`
- **No git commit or publication performed.**

## Version
- Package version: **1.3.0**
- Runtime dependency: `golded-ftn>=1.3.0,<2`
- `[tool.uv.sources] golded-ftn = {path = "../golded-ftn-python-r1", editable = true}`

## Summary of changes
1. **`SquishWriter.recovery_session`** implements `RecoverableMessageBaseWriter`.
   - Locks `<base>.sqd` byte 0 via `locks.acquire` and holds that descriptor across `pending()`, `recover()`, `prepare_append()`, `prepare_update()`, `apply()`, and `settle()`.
   - Opens `.sqi` only after the `.sqd` lock, with `O_NOFOLLOW` / `O_NOCTTY`. Symlink `.sqd`/`.sqi` and `fstat`/`lstat` mismatches fail closed as `RecoveryError(code="unsafe_recovery_path")`.
   - `rel_path` stays the on-disk basename (not a realpath-resolved file path).
   - Physical base: `format="squish"`, `canonical_key` = realpath of the verified `.sqd`, `lock_inode` of the locked `.sqd`.
   - Plans include free-frame/message-chain header bytes and index truncation, including the truncated `.sqi` tail (`(0, max(len(old), len(new)))`). `.sql` lastread is never opened or written.
   - Adapter-owned `os.open` / directory-fsync `OSError`s are re-raised as closed-code `RecoveryError` with `from None`; `str(error)` has no filesystem paths.
   - Does not re-enter public per-operation lock methods. `concurrent=True` is rejected (C0-03).
2. **Date adapter** converts naive local wall time using `TZUTC` or `SquishWriterOptions.timezone` (IANA) to UTC **before** year-bound checks.
   - Aware `posted_at` is rejected (`SquishAdapterError("aware_datetime")`).
   - A `control_lines` patch that drops `TZUTC` does not reuse the original `TZUTC`; area timezone / DST HELD applies.
   - Missing area timezone, malformed/conflicting `TZUTC`, DST gap/fold, and UTC years outside **1980–2069** raise `SquishAdapterError` (tosser HELD). Years 2070–2107 are rejected so two-digit ASCII dates cannot wrap to 1970–1999.
   - Odd DOS seconds remain floored; ASCII seconds stay exact.
3. **`prepare_*` order:** `check_usable` + `require_binding` + active-mutation / pending-artifact checks run before serialize/validate/load, so `missing_recovery_binding` and `invalid_prepared_mutation` are not masked by adapter errors.
4. **Stable UIDs and raw reply fields** are unchanged. Reply slots are stored as supplied UIDs; no reply translation is invented.
5. **Errors** use fixed English strings with no filesystem paths or message content. Recovery artifacts use engine `0o700` / `0o600` permissions.
6. Existing offline CRUD (`open`/`append`/`update`/`delete`) remains on per-operation lock + in-place `Transaction` rollback. The RollbackError regex was updated for R1 sanitization (`Transaction rollback failed during <operation>`).

## Coordinator review decisions applied
- **B1:** Adapter-owned `os.open` / directory fsync `OSError` → `RecoveryError` closed code, `from None`, no paths in `str(error)` (including `apply_fn`).
- **B2:** `.sqi` opened after `.sqd` lock; `O_NOFOLLOW`; symlink / `fstat!=lstat` → `unsafe_recovery_path`; files themselves are not realpath-resolved into `rel_path`.
- **D1:** `.sqi` planned range covers truncation, including an empty new index over old bytes.
- **D3:** Aware `posted_at` rejected.
- **D4:** Dropped `TZUTC` in a patch does not fall back to the original `TZUTC`.
- **D5:** Binding / active-mutation checks at the start of `prepare_*`.
- **D7:** Missing timezone, bad/conflicting `TZUTC`, and out-of-range UTC year are `SquishAdapterError`. Stored ASCII year capped to the k1 1970–2069 window (writer bound 1980–2069).

## Files changed
- `src/golded_ftn_squish/writer.py`
- `tests/test_recovery.py`
- `tests/test_writer.py`
- `evidence/handoff.md`

Earlier 1.3.0 recovery work also touched `pyproject.toml`, `uv.lock`, `CHANGELOG.md`, `scripts/verify_distribution.py`, `src/golded_ftn_squish/__init__.py`, and `tests/test_readme.py`.

## Verification
```
.venv/bin/pytest -q
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy --strict src tests scripts
```

| Check | Result |
| --- | --- |
| pytest | **176 passed, 1 skipped** (177 collected; skip is the existing case-insensitive filesystem reader probe) |
| ruff check | All checks passed |
| ruff format --check | 11 files already formatted |
| mypy --strict | Success: no issues found in 13 source files |

Covered F-Squish tests:
- prepare_append / apply / settle(`COMMITTED`) updates `.sqd`/`.sqi` while the `.sqd` lock is held
- settle(`UNCOMMITTED`) restores chains and index
- recover `UNCOMMITTED` after an unsettled apply
- `.sql` lastread unchanged
- UID stable across live uncommitted settle and across recover
- DST gap without `TZUTC` raises; explicit `TZUTC` accepted; DST fold raises
- UTC year bounds after offset conversion (1980–2069); year 2070 is `SquishAdapterError`; odd seconds floored
- symlink `.sqi` → `unsafe_recovery_path` (opened only after lock)
- apply directory-fsync `OSError` has no path in `str(error)` / `repr(error)`
- shorter `.sqi` (truncated slack) restores on `UNCOMMITTED`
- aware `posted_at` rejected
- dropped `TZUTC` in a patch uses area timezone / DST HELD
- missing `.base-binding.json` wins over a DST input on `prepare_append`
- existing CRUD tests still pass; `concurrent=True` still rejected

## Remaining gaps
- Recovery kill/crash-injection matrix at each `.sqd`/`.sqi` write boundary is not added (existing CRUD SIGKILL probe remains).
- `prepare_update` recover-`UNCOMMITTED` after process restart is not a dedicated test; live update `COMMITTED` plus append recover coverage are.
- GoldED concurrent profile remains rejected (C0-03 unlock-before-persist). No C1 barrier test.
- **D6** (deferred): `canonical_key` is still `os.path.realpath` of the caller spelling; not case-normalized on case-insensitive volumes.
- **S2–S5** (deferred): `MessageIdentity.base` path in `repr()`, reader `_require`/`_slice` diagnostics, directory reopen policy, and CRUD `FileExistsError` path strings.
- `scripts/verify_distribution.py` was not executed in this task.
- Windows/Linux execution was not run here.
