# Changelog

## 1.3.0 (unreleased)

- Add recoverable sessions and existing-case resolution. Fix options initialization on Python 3.12; retain stable UIDs, timezone policy and rollback.

## 1.3.0 — 2026-10-17

Add a lock-retaining recoverable writer session for Squish `.sqd`/`.sqi` mutation
with durable prepare/apply/settle recovery. Convert naive local wall time using
`TZUTC` or a configured IANA area timezone before UTC year bounds. DST gaps and
folds without explicit `TZUTC` fail closed. Concurrent GoldED access remains
rejected. Lastread `.sql` files stay untouched.

## 1.2.0 — 2026-10-05

Add classic Squish create, append, update and delete sessions. Preserve UIDs and
raw metadata, maintain active and free chains, validate the complete base before
mutation, and restore files in place after ordinary write failures. GoldED
concurrent access remains disabled pending build tests.

Preserve omitted routing during body edits and omitted MSGID during control-block replacement. Reject conflicting MSGID controls.

## 1.1.0 — Unreleased

Add reported archive reading with index-authoritative UIDs, empty control
segment recovery and strict ASCII fallback. Bad indexed records are skipped;
ambiguous index order or overlap stops traversal. Strict reading remains the default.

## 1.0.0 — 2026-10-04

- Read classic Squish areas through the active index prefix, preserving UIDs.
- Validate file bounds, frames, controls and strict decoding before returning.
- Preserve body, addresses, IDs, dates, reply links and source provenance.
- Include synthetic fixtures, portable CI and isolated distribution checks.
