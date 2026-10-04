# Changelog

## 1.1.0 — Unreleased

Add reported archive reading with index-authoritative UIDs, empty control
segment recovery and strict ASCII fallback. Bad indexed records are skipped;
ambiguous index order or overlap stops traversal. Strict reading remains the default.

## 1.0.0 — 2026-10-04

- Read classic Squish areas through the active index prefix, preserving UIDs.
- Validate file bounds, frames, controls and strict decoding before returning.
- Preserve body, addresses, IDs, dates, reply links and source provenance.
- Include synthetic fixtures, portable CI and isolated distribution checks.
