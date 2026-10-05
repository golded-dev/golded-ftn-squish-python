# Squish writer source notes

The reference checkout is `golded-open-source`, commit
`600266252b73174ff5116cee697ef9a97aeb1859`, with paths relative to that repo.
This implementation uses independent binary fixtures. It has not been exercised
against a running GoldED build.

- `goldlib/gmb3/gmosqsh.h:154-244`: nine reply slots, 238-byte message headers,
  28-byte frame headers, 12-byte index entries and the 256-byte base record.
- `goldlib/gmb3/gmosqsh2.cpp:38-74`: refresh, initial next UID 2 and frame sizes.
- `goldlib/gmb3/gmosqsh4.cpp:38-82`: byte-0 lock and unlock-before-base/index-write.
  That ordering prevents a concurrency claim based on matching write locks alone.
- `goldlib/gmb3/gmosqsh4.cpp:129-203`: deletion, active-chain unlinking and free list.
- `goldlib/gmb3/gmosqsh4.cpp:336-641`: message/header serialization, UID assignment,
  frame allocation, recipient index hash and control/text boundaries. Python
  always appends replacement content frames rather than recycling free frames.
- `goldlib/gall/gcrchash.cpp:40-61`: case-insensitive recipient hash and read flag.
- `goldlib/gmb3/gmosqsh1.cpp:201-218`: `.SQL` lastread slots. Editing preserves them.
- `goldlib/gmb3/gmosqsh2.cpp:88-250`: scan modes, index caching and reread behavior.

The local macOS tests cover independent fixtures, revision conflicts, two sessions,
two processes, controlled lock timeout and injected mutation failures. Windows
byte locks have an implementation but no live platform test in this run. Linux
execution is also unverified. GoldED
commit/compiler/flags/configuration and runtime refresh checks remain pending.
All platforms reject `concurrent=True`.

A deterministic POSIX process-kill probe terminates a child immediately after
its first appended-frame write. The old header and index remain unchanged and
the appended bytes remain at the end of `.SQD`. The standalone reader returns
the old indexed message; the writer rejects the inconsistent end-frame marker.
This is one observed boundary, not proof that every interrupted write is detected.
