# golded-ftn-squish

This Python package reads classic Squish areas through golded-ftn models. The
base header's active count selects the SQI prefix; ignore its allocated tail.
Use persistent index UIDs for msgno and reply links. Preserve body separately
from header controls, and require stable areas during the two file reads.

Validate binary bounds before decoding. Preserve filesystem errors; chain parser
failures with actual paths and offsets. Decode strictly through core helpers.
Use synthetic, independently constructed binary fixtures for changed behavior.
Keep writers, discovery, databases and core changes outside this reader's scope.

Run README checks and scripts/verify_distribution.py. Distributions use the public
core dependency; local uv sources belong only to development. Edit this fragment
or agent-compose.toml, then preview, build and check. Private persona sources stay
outside distributions. Commit and publication require an explicit request.
